#!/bin/bash
# ======================================================
# 🛡️ AZHOME SAAS - OPTIMIZED INDIVIDUAL BACKUP 🛡️
# 🚀 Backup từng Database + Filestore (No Owner, .backup format)
# ======================================================

# --- [ CẤU HÌNH ] ---
PROJECT_DIR="/volume1/docker/azhome_main" 
BACKUP_ROOT="$PROJECT_DIR/backups"
DB_CONTAINER="db_master"
DB_USER="odoo"
DB_PASS="odoo_saas_super_secret"
MAX_DAYS=7 

# Tạo danh mục theo Ngày_Giờ
DATE=$(date +"%Y%m%d_%H%M%S")
DAILY_DIR="$BACKUP_ROOT/$DATE"
mkdir -p "$DAILY_DIR"

# Thư mục tạm để đóng gói
TMP_ROOT="/tmp/az_bk_$DATE"
mkdir -p "$TMP_ROOT"

echo "======================================================"
echo "🚀 KHỞI CHẠY BACKUP TỐI ƯU: $DATE"
echo "======================================================"

# 1. LẤY DANH SÁCH DATABASE
# Lọc chính xác các DB (Loại bỏ các database hệ thống)
DB_LIST=$(docker exec -e PGPASSWORD="$DB_PASS" "$DB_CONTAINER" psql -U "$DB_USER" -d postgres -t -c "SELECT datname FROM pg_database WHERE datistemplate = false AND datname NOT IN ('postgres', 'template1', 'postgres_exporter');" | tr -d '\r' | xargs)

if [ -z "$DB_LIST" ]; then
    echo "❌ LỖI: Không quét được Database nào!"
else
    for DB in $DB_LIST; do
        echo "------------------------------------------------------"
        echo "📂 Đang xử lý: [$DB]"
        TENANT_TMP="$TMP_ROOT/$DB"
        mkdir -p "$TENANT_TMP"
        
        # A. Dump Database (Dùng định dạng Custom .backup)
        # --no-owner: Loại bỏ quyền sở hữu
        # --no-privileges: Loại bỏ các quyền truy cập (ACL)
        # -Fc: Định dạng Custom (chuẩn .backup)
        # Lưu file vào /tmp bên trong container rồi mới copy ra để tránh lỗi ký tự binary trên shell
        docker exec -e PGPASSWORD="$DB_PASS" "$DB_CONTAINER" pg_dump -U "$DB_USER" -d "$DB" --no-owner --no-privileges -Fc -f "/tmp/dump.backup"
        docker cp "$DB_CONTAINER:/tmp/dump.backup" "$TENANT_TMP/dump.backup"
        docker exec "$DB_CONTAINER" rm "/tmp/dump.backup"
        echo "   ✅ SQL Backup (Custom Format .backup) xong."

        # B. Hút Filestore (Lọc đường dẫn chính xác)
        if [[ $DB == azhome_tenant_* ]]; then
            # Nếu là Tenant, lấy từ container của chính nó (Isolation)
            PREFIX=${DB#azhome_tenant_}
            CONT_NAME=$(docker ps --all --filter "name=odoo_tenant_$PREFIX" --format "{{.Names}}" | head -n 1)
            if [ ! -z "$CONT_NAME" ]; then
                docker cp "$CONT_NAME:/var/lib/odoo/filestore/$DB" "$TENANT_TMP/filestore" 2>/dev/null
                echo "   ✅ Filestore (Tenant Container) xong."
            else
                echo "   ⚠️ CẢNH BÁO: Không thấy container odoo_tenant_$PREFIX, bỏ qua filestore."
            fi
        else
            # Nếu là Master hoặc Dev, lấy từ Volume Master
            if [ -d "$PROJECT_DIR/master_data/filestore/$DB" ]; then
                cp -r "$PROJECT_DIR/master_data/filestore/$DB" "$TENANT_TMP/filestore"
                echo "   ✅ Filestore (Master Volume) xong."
            else
                echo "   ⚠️ CẢNH BÁO: Không thấy thư mục filestore trong Master Volume cho $DB."
            fi
        fi
        
        # C. Đóng gói chuẩn .tar (Nén lại để dễ di chuyển)
        tar -cf "$DAILY_DIR/${DB}.tar" -C "$TENANT_TMP" .
        echo "   ✅ Đóng gói ${DB}.tar hoàn tất."
        
        # Dọn dẹp thư mục tạm của DB này
        rm -rf "$TENANT_TMP"
    done
fi

# 2. BACKUP MÃ NGUỒN VÀ CẤU HÌNH (Rất quan trọng cho Dev)
echo "------------------------------------------------------"
echo "📦 Đóng gói Source Code và Docker Config..."
tar -czf "$DAILY_DIR/00_source_code_and_config.tar.gz" \
    -C "$PROJECT_DIR" az_addons_cons az_addons_saas docker_build
echo "   ✅ Xong."

# 3. DỌN DẸP
echo "------------------------------------------------------"
echo "🧹 Quét dọn rác và file cũ hơn $MAX_DAYS ngày..."
rm -rf "$TMP_ROOT"
sync

# Xóa các thư mục backup cũ
find "$BACKUP_ROOT" -maxdepth 1 -type d -mtime +$MAX_DAYS -not -path "$BACKUP_ROOT" -exec rm -rf {} \;

echo "======================================================"
echo "✅ HOÀN TẤT BACKUP!"
echo "📍 Vị trí: $DAILY_DIR"
echo "======================================================"
