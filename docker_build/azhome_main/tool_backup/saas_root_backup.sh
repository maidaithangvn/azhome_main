#!/bin/bash
# ==========================================
# 🛡️ AZHOME SAAS - ALL-IN-ONE BACKUP 🛡️
# 🚀 Tự động nhận diện TOÀN BỘ Database
# ==========================================

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

# Thư mục tạm
TMP_ROOT="/tmp/az_bk_$DATE"
mkdir -p "$TMP_ROOT"

echo "======================================"
echo "🚀 KHỞI CHẠY BACKUP TOÀN DIỆN: $DATE"
echo "======================================"

# 1. LẤY DANH SÁCH DATABASE (Lấy tất cả trừ database hệ thống)
echo "🔍 Đang quét toàn bộ hệ thống Database..."
DB_LIST=$(docker exec -e PGPASSWORD="$DB_PASS" "$DB_CONTAINER" psql -U "$DB_USER" -d postgres -t -c "SELECT datname FROM pg_database WHERE datistemplate = false AND datname NOT IN ('postgres', 'template1');" | tr -d '\r' | xargs)

if [ -z "$DB_LIST" ]; then
    echo "❌ LỖI: Không quét được Database nào!"
else
    for DB in $DB_LIST; do
        printf "📂 [%-20s] -> " "$DB"
        TENANT_TMP="$TMP_ROOT/$DB"
        mkdir -p "$TENANT_TMP"
        
        # A. Dump SQL
        docker exec -e PGPASSWORD="$DB_PASS" "$DB_CONTAINER" pg_dump -U "$DB_USER" -d "$DB" > "$TENANT_TMP/dump.sql"
        printf "SQL ok, "

        # B. Hút Filestore (Tenant vs Master/Dev)
        if [[ $DB == azhome_tenant_* ]]; then
            # Hút từ container Tenant
            PREFIX=${DB#azhome_tenant_}
            CONT_NAME=$(docker ps --all --filter "name=odoo_tenant_$PREFIX" --format "{{.Names}}" | head -n 1)
            if [ ! -z "$CONT_NAME" ]; then
                docker cp "$CONT_NAME:/var/lib/odoo/filestore/$DB" "$TENANT_TMP/filestore" 2>/dev/null
                printf "Filestore container ok, "
            fi
        else
            # Hút từ Master Data (Dành cho Master/Dev và các DB khác)
            if [ -d "$PROJECT_DIR/master_data/filestore/$DB" ]; then
                cp -r "$PROJECT_DIR/master_data/filestore/$DB" "$TENANT_TMP/filestore"
                printf "Filestore master volume ok, "
            fi
        fi
        
        # C. Nén phẳng (Chuẩn Odoo Restore)
        tar -czf "$DAILY_DIR/${DB}.tar.gz" -C "$TENANT_TMP" .
        printf "Zip ok.\n"
        
        rm -rf "$TENANT_TMP"
    done
fi

# 2. BACKUP MÃ NGUỒN VÀ DOCKER CONFIG
echo "📦 Đóng gói Source Code và Docker Config..."
tar -czf "$DAILY_DIR/00_source_code_and_config.tar.gz" \
    -C "$PROJECT_DIR" az_addons_cons az_addons_saas docker_build

# 3. DỌN DẸP
echo "🧹 Quét dọn rác và file cũ..."
rm -rf "$TMP_ROOT"
sync

# Xóa các thư mục backup cũ hơn MAX_DAYS ngày
find "$BACKUP_ROOT" -maxdepth 1 -type d -mtime +$MAX_DAYS -not -path "$BACKUP_ROOT" -exec rm -rf {} \;

echo "✅ HOÀN TẤT!"
echo "📍 Tọa độ: $DAILY_DIR"
echo "======================================"
