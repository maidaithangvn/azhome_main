import os
import logging
from odoo import models, fields, api, _
from odoo.exceptions import UserError
from markupsafe import Markup

_logger = logging.getLogger(__name__)

try:
    import docker
except ImportError:
    docker = None
    _logger.warning(
        "Thư viện 'docker' (docker-py) chưa được cài đặt. Vui lòng chạy: pip install docker"
    )

# ============================================================
#  PHÁT HIỆN NỀN TẢNG (Windows / Linux NAS / Linux VPS)
# ============================================================
IS_LINUX = os.name == "posix"
IS_WINDOWS = os.name == "nt"

# Đường dẫn mặc định theo quy ước thư mục mới:
#   - Windows:    .../server/azhome_main/az_addons_cons
#   - Linux Main: /volume1/docker/azhome_main/az_addons_cons
_DEFAULT_CONS_ADDONS_PATH = (
    "/volume1/docker/azhome_main/az_addons_cons"
    if IS_LINUX
    else "F:/THANG2022/ODOO19_2026/server/azhome_main/az_addons_cons"
)

# Đường dẫn chứa dữ liệu Filestore cho từng khách hàng
_DEFAULT_TENANT_DATA_ROOT = (
    "/volume1/docker/azhome_main/tenant_data"
    if IS_LINUX
    else "F:/THANG2022/ODOO19_2026/server/azhome_main/tenant_data"
)


class SaasTenant(models.Model):
    _name = "saas.tenant"
    _description = "Khách hàng SaaS"
    _inherit = ["mail.thread", "mail.activity.mixin"]

    # === THÔNG TIN KHÁCH HÀNG ===
    partner_id = fields.Many2one(
        "res.partner", string="Khách hàng (Odoo Contact)", required=True, tracking=True
    )
    name = fields.Char(
        related="partner_id.name", string="Tên Công ty/Khách hàng", store=True
    )
    domain_prefix = fields.Char(string="Prefix Domain", required=True, tracking=True)
    email = fields.Char(
        related="partner_id.email", string="Email Liên hệ", readonly=False
    )
    phone = fields.Char(related="partner_id.phone", string="Điện thoại", readonly=False)

    user_id = fields.Many2one(
        "res.users",
        string="Nhân viên phụ trách",
        default=lambda self: self.env.user,
        tracking=True,
    )

    tenant_domain = fields.Char(
        string="Domain đầy đủ", compute="_compute_domain", store=True
    )

    # === HẠ TẦNG DOCKER ===
    db_name = fields.Char(string="Tên Database", readonly=True)
    container_id = fields.Char(string="Docker Container ID", readonly=True)
    port = fields.Integer(string="Cổng TCP (Local)", readonly=True)

    # === THỐNG KÊ TÀI NGUYÊN ===
    current_users_count = fields.Integer(
        string="Số User đang dùng", default=0, readonly=True
    )
    max_users_display = fields.Integer(
        string="Giới hạn User", related="plan_id.max_users", readonly=True
    )
    disk_usage_mb = fields.Float(
        string="Dung lượng đã dùng (MB)", default=0, readonly=True
    )
    max_disk_display = fields.Integer(
        string="Hạn mức Lưu trữ (GB)", related="plan_id.disk_storage", readonly=True
    )
    stats_last_updated = fields.Datetime(string="Cập nhật lần cuối", readonly=True)

    # === GÓI CƯỚC ===
    plan_id = fields.Many2one(
        "saas.plan", string="Khách Mua Gói", required=True, tracking=True
    )
    expiry_date = fields.Date(string="Ngày hết hạn", tracking=True)

    state = fields.Selection(
        [
            ("draft", "Mới"),
            ("running", "Đang hoạt động"),
            ("stopped", "Tạm dừng"),
            ("error", "Lỗi khởi tạo"),
        ],
        string="Trạng thái",
        default="draft",
        tracking=True,
    )

    setup_modules = fields.Char(
        string="Mô-đun Khởi tạo",
        default="base,mail,hr,project,sale_management,az_construction_management,azhome_saas",
    )

    _domain_prefix_uniq = models.Constraint(
        'unique(domain_prefix)',
        'Prefix này đã tồn tại!',
    )

    # ========================================================
    #  COMPUTED / ONCHANGE
    # ========================================================
    @api.onchange("plan_id")
    def _onchange_plan_id(self):
        if self.plan_id and self.plan_id.duration_days > 0:
            from datetime import date, timedelta

            self.expiry_date = date.today() + timedelta(days=self.plan_id.duration_days)

    @api.depends("domain_prefix")
    def _compute_domain(self):
        for rec in self:
            if rec.domain_prefix:
                rec.tenant_domain = f"{rec.domain_prefix.lower().strip()}.azhome.top"
            else:
                rec.tenant_domain = False

    # ========================================================
    #  HELPER: Lấy đường dẫn Addons trên Host (cho Tenant)
    # ========================================================
    def _get_tenant_cons_addons_path(self):
        """Trả về đường dẫn thực trên Host OS để mount vào Container Tenant."""
        return self.env["ir.config_parameter"].sudo().get_param(
            "azhome_saas.tenant_cons_addons_path", _DEFAULT_CONS_ADDONS_PATH
        )

    def _get_tenant_data_host_path(self):
        """Trả về đường dẫn tới thư mục data của Tenant trên Host."""
        root = self.env["ir.config_parameter"].sudo().get_param(
            "azhome_saas.tenant_data_root", _DEFAULT_TENANT_DATA_ROOT
        )
        # Mỗi tenant một folder riêng theo slug
        path = os.path.join(root, self._get_tenant_slug())
        if not os.path.exists(path):
            try:
                os.makedirs(path, exist_ok=True)
            except Exception:
                pass
        return path

    def _get_tenant_slug(self):
        """Chuẩn hóa tên slug cho container/database."""
        return self.domain_prefix.lower().strip().replace(" ", "")

    # ========================================================
    #  CRON: Quét Tenant sắp hết hạn (30 ngày)
    # ========================================================
    @api.model
    def _cron_check_tenant_expiry(self):
        """Cronjob quét tenant sắp hết hạn (30 ngày)."""
        from datetime import date, timedelta

        limit_date = date.today() + timedelta(days=30)
        tenants_expiring = self.search(
            [("state", "in", ["running", "stopped"]), ("expiry_date", "<=", limit_date)]
        )
        for tenant in tenants_expiring:
            # Gán cho nhân viên phụ trách hoặc người chạy cron
            responsible_user_id = tenant.user_id.id or self.env.user.id
            
            # Kiểm tra xem đã có activity tương tự chưa để tránh lặp lại mỗi ngày
            existing_activity = self.env['mail.activity'].search([
                ('res_model', '=', self._name),
                ('res_id', '=', tenant.id),
                ('summary', 'like', 'Sắp hết hạn Gói SaaS'),
                ('user_id', '=', responsible_user_id)
            ], limit=1)
            
            if not existing_activity:
                tenant.activity_schedule(
                    "mail.mail_activity_data_todo",
                    summary=f"⚠️ Sắp hết hạn Gói SaaS: {tenant.partner_id.name}",
                    note=f"<p>Khách hàng <b>{tenant.name}</b> sắp hết hạn gói <b>{tenant.plan_id.name}</b> vào ngày <b>{tenant.expiry_date}</b>.</p>"
                         f"<p>Vui lòng liên hệ để gia hạn.</p>",
                    user_id=responsible_user_id,
                    date_deadline=tenant.expiry_date,
                )
                tenant.message_post(
                    body=Markup(f"⏰ <b>Hệ thống:</b> Đã tạo Việc cần làm nhắc nhở hết hạn (30 ngày).")
                )

    # ========================================================
    #  CRUD OVERRIDE
    # ========================================================
    def unlink(self):
        """Ngăn chặn việc vô tình xóa bản ghi khi Tenant đang chạy."""
        for rec in self:
            if rec.state == "running":
                raise UserError(
                    _(
                        "Không thể xóa Khách hàng đang ở trạng thái 'Đang hoạt động'.\n"
                        "Vui lòng chạy chức năng [Hủy Dữ liệu] để dọn sạch Container/Database trên máy chủ trước, "
                        "hệ thống sẽ trả về trạng thái Nháp rồi bạn mới có thể xóa sổ bản ghi này."
                    )
                )
        return super(SaasTenant, self).unlink()

    # ========================================================
    #  CẬP NHẬT THỐNG KÊ (Master pull từ Container qua HTTP)
    # ========================================================
    def action_refresh_stats(self):
        """Master chủ động query Tenant Odoo qua HTTP JSON-RPC để lấy số user & dung lượng."""
        self.ensure_one()
        if self.state != "running" or not self.port:
            raise UserError(_("Tenant chưa hoạt động hoặc chưa có cổng kết nối."))

        import requests as req

        tenant_slug = self._get_tenant_slug()
        container_name = f"odoo_tenant_{tenant_slug}_{self.port}"

        # Thử các URL để kết nối:
        # 1. host.docker.internal (Nếu Master chạy trong Docker trên Windows)
        # 2. localhost (Nếu Master chạy trực tiếp trên Host Windows/Linux)
        # 3. Container Name (Nếu Master và Tenant cùng network az_saas_network)
        urls_to_try = [
            f"http://host.docker.internal:{self.port}/azhome/tenant_stats",
            f"http://localhost:{self.port}/azhome/tenant_stats",
            f"http://{container_name}:8069/azhome/tenant_stats",
            f"http://127.0.0.1:{self.port}/azhome/tenant_stats",
        ]

        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"token": self.id},
        }

        resp = None
        for url in urls_to_try:
            try:
                resp = req.post(url, json=payload, timeout=15)
                break
            except req.exceptions.ReadTimeout:
                # Tenant đang bận (khởi tạo modules) → thông báo thân thiện
                self.message_post(
                    body=Markup(
                        "⏳ <b>Tenant đang khởi tạo...</b><br/>"
                        "Hệ thống đang cài đặt modules cho Tenant. "
                        "Vui lòng đợi 3-5 phút rồi thử lại."
                    )
                )
                raise UserError(
                    _(
                        "⏳ Tenant đang bận khởi tạo (cài đặt modules).\n\n"
                        "Quá trình này thường mất 3-5 phút sau khi tạo mới.\n"
                        "Vui lòng đợi rồi bấm lại nút [Cập nhật Thống kê].\n\n"
                        "💡 Mẹo: Bấm nút [📋 Xem Log Container] để theo dõi tiến độ cài đặt."
                    )
                )
            except req.exceptions.ConnectionError:
                continue

        if not resp:
            raise UserError(
                _(
                    "⚠️ Không thể kết nối đến Tenant.\n\n"
                    "Cổng: %s | Container: %s\n\n"
                    "Nguyên nhân có thể:\n"
                    "• Container đang khởi tạo (đợi 3-5 phút)\n"
                    "• Container không chung mạng az_saas_network\n"
                    "• Container đã bị dừng\n\n"
                    "💡 Bấm [📋 Xem Log Container] để kiểm tra."
                )
                % (self.port, container_name)
            )

        try:
            result = resp.json()

            if "error" in result:
                raise UserError(
                    _("Tenant trả về lỗi: %s") % result["error"].get("message")
                )

            data = result.get("result", {})
            if data.get("status") == "error":
                raise UserError(_("Lỗi từ Tenant: %s") % data.get("message"))

            stats = data.get("stats", {})
            user_count = stats.get("user_count", 0)
            disk_mb = stats.get("disk_usage_mb", 0)

            self.write(
                {
                    "current_users_count": user_count,
                    "disk_usage_mb": disk_mb,
                    "stats_last_updated": fields.Datetime.now(),
                }
            )

            self.message_post(
                body=Markup(
                    "📊 <b>Cập nhật thống kê (Pull)!</b><br/>"
                    "👤 User: <b>%s</b> / %s &nbsp;|&nbsp; 💾 Disk: <b>%s MB</b>"
                )
                % (user_count, self.plan_id.max_users, disk_mb)
            )

        except UserError:
            raise  # Giữ nguyên UserError đã format ở trên
        except Exception as e:
            raise UserError(_("Lỗi khi xử lý dữ liệu thống kê: %s") % str(e))

    # ========================================================
    #  GIA HẠN GÓI CƯỚC
    # ========================================================
    def action_renew_subscription(self):
        """Gia hạn thêm N ngày theo gói cước hiện tại."""
        self.ensure_one()
        if not self.plan_id or not self.plan_id.duration_days:
            raise UserError(
                _("Gói cước chưa có thời hạn. Vui lòng kiểm tra cấu hình Gói.")
            )

        from datetime import timedelta

        base_date = self.expiry_date or fields.Date.today()
        new_expiry = base_date + timedelta(days=self.plan_id.duration_days)
        old_expiry = self.expiry_date

        self.write({"expiry_date": new_expiry})
        self._push_env_to_container({"AZHOME_EXPIRY_DATE": str(new_expiry)})

        self.message_post(
            body=Markup(
                "🔄 <b>GIA HẠN thành công!</b><br/>"
                "Gói: <b>%s</b> &nbsp;|&nbsp; Hạn cũ: %s → Hạn mới: <b>%s</b> (+%s ngày)"
            )
            % (
                self.plan_id.name,
                old_expiry or "N/A",
                new_expiry,
                self.plan_id.duration_days,
            )
        )

    # ========================================================
    #  NÂNG CẤP GÓI CƯỚC (Wizard)
    # ========================================================
    def action_upgrade_plan(self):
        """Mở wizard để chọn gói mới."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Nâng - Hạ / Đổi Gói cước",
            "res_model": "saas.tenant.upgrade.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_tenant_id": self.id,
                "default_current_plan_id": self.plan_id.id,
            },
        }

    def _apply_plan_upgrade(self, new_plan):
        """Áp dụng gói mới lên Tenant."""
        self.ensure_one()
        old_plan = self.plan_id
        from datetime import timedelta

        new_expiry = fields.Date.today() + timedelta(days=new_plan.duration_days)

        self.write(
            {
                "plan_id": new_plan.id,
                "expiry_date": new_expiry,
            }
        )

        self._push_env_to_container(
            {
                "AZHOME_MAX_USERS": str(new_plan.max_users),
                "AZHOME_PLAN_CODE": str(new_plan.code),
                "AZHOME_EXPIRY_DATE": str(new_expiry),
            }
        )

        self.message_post(
            body=Markup(
                "⬆️⬇️ <b>THAY ĐỔI GÓI!</b><br/>"
                "Cũ: %s (%s users) → Mới: <b>%s</b> (<b>%s</b> users)<br/>"
                "Hết hạn mới: <b>%s</b>"
            )
            % (
                old_plan.name,
                old_plan.max_users,
                new_plan.name,
                new_plan.max_users,
                new_expiry,
            )
        )

    # ========================================================
    #  PUSH ENV VÀO CONTAINER
    # ========================================================
    def _push_env_to_container(self, env_updates):
        """Ghi biến môi trường mới vào Container qua docker exec."""
        if not docker or not self.container_id:
            return
        try:
            client = docker.from_env()
            tenant_slug = self._get_tenant_slug()
            containers = client.containers.list(
                filters={"name": f"odoo_tenant_{tenant_slug}"}
            )
            if not containers:
                return
            env_lines = "\\n".join([f"{k}={v}" for k, v in env_updates.items()])
            containers[0].exec_run(f"sh -c \"echo -e '{env_lines}' > /etc/azhome_env\"")
            _logger.info(
                f"[SaaS] Đã push ENV vào Container: {list(env_updates.keys())}"
            )
        except Exception as e:
            _logger.warning(f"[SaaS] Lỗi push ENV: {e}")

    # ========================================================
    #  PROVISION & LIFECYCLE
    # ========================================================
    def action_provision_tenant(self):
        """Tạo Odoo Container mới qua Docker Engine API.
        Tương thích cả NAS (Xpennology) và VPS Production.
        """
        self.ensure_one()
        if not docker:
            raise UserError(
                _("Server thiếu thư viện 'docker'. Cần: pip install docker")
            )
        if self.state not in ["draft", "error"]:
            raise UserError(_("Tiến trình khởi tạo đã được thực thi."))

        try:
            client = docker.from_env()
        except Exception as e:
            raise UserError(_("Không thể kết nối Docker: %s") % str(e))

        tenant_slug = self._get_tenant_slug()
        
        # --- CẤU HÌNH DATABASE (Động cho Windows/Linux) ---
        conf = self.env["ir.config_parameter"].sudo()
        
        # Mặc định trên Windows dùng host.docker.internal, trên Linux dùng db_master (Docker network)
        default_db_host = "host.docker.internal" if IS_WINDOWS else "db_master"
        
        db_host = conf.get_param("azhome_saas.tenant_db_host", default_db_host)
        db_user = conf.get_param("azhome_saas.tenant_db_user", "openpg" if IS_WINDOWS else "odoo")
        db_password = conf.get_param("azhome_saas.tenant_db_password", "openpgpwd" if IS_WINDOWS else "odoo_saas_super_secret")
        db_name = f"azhome_tenant_{tenant_slug}"

        # --- Traefik Labels ---
        labels = {
            "traefik.enable": "true",
            "saas": "true",
            f"traefik.http.routers.{tenant_slug}.rule": f"Host(`{self.tenant_domain}`)",
            f"traefik.http.services.{tenant_slug}.loadbalancer.server.port": "8069",
        }

        # --- Port ---
        import random

        assigned_port = random.randint(10000, 20000)

        # --- Environment ---
        env_vars = {
            "HOST": db_host,
            "USER": db_user,
            "PASSWORD": db_password,
            "ODOO_MAX_CRON_THREADS": "1",
            "AZHOME_MAX_USERS": str(self.plan_id.max_users),
            "AZHOME_PLAN_CODE": str(self.plan_id.code),
            "AZHOME_TENANT_ID": str(self.id),
            "AZHOME_EXPIRY_DATE": str(self.expiry_date),
            "AZHOME_MASTER_URL": self.env["ir.config_parameter"]
            .sudo()
            .get_param("web.base.url", "http://host.docker.internal:8069"),
        }

        # --- Volume Mounts (Chuẩn hóa theo cấu trúc mới) ---
        cons_addons_path = self._get_tenant_cons_addons_path()
        tenant_data_path = self._get_tenant_data_host_path()
        
        docker_mounts = {
            cons_addons_path: {"bind": "/mnt/extra-addons", "mode": "ro"},
            tenant_data_path: {"bind": "/var/lib/odoo", "mode": "rw"},
        }
        _logger.info(
            f"[SaaS] Tenant mounts: Addons={cons_addons_path}, Data={tenant_data_path}"
        )

        # --- Command ---
        cmd_args = (
            f"odoo -d {db_name} -i {self.setup_modules} "
            f"--load-language=vi_VN --db-filter=^{db_name}$ "
            f"--no-database-list --without-demo=all"
        )

        # --- Resource Limits ---
        mem_limit_bytes = self.plan_id.mem_limit or "1024m"

        # nano_cpus: Thử áp dụng trên Linux. Nếu kernel không hỗ trợ CFS scheduler
        # (Xpennology, một số NAS), Docker sẽ báo lỗi → tự động retry không có nano_cpus.
        nano_cpus_limit = None
        if IS_LINUX and self.plan_id.cpu_limit:
            nano_cpus_limit = int(self.plan_id.cpu_limit) * 1_000_000_000

        # --- Docker Run ---
        run_kwargs = dict(
            image="odoo:19",
            name=f"odoo_tenant_{tenant_slug}_{assigned_port}",
            environment=env_vars,
            command=cmd_args,
            volumes=docker_mounts,
            labels=labels,
            ports={"8069/tcp": assigned_port},
            network="az_saas_network",
            detach=True,
            mem_limit=mem_limit_bytes,
            restart_policy={"Name": "unless-stopped"},
            extra_hosts={"host.docker.internal": "host-gateway"} if IS_WINDOWS else {},
        )

        try:
            # Lần 1: Thử với CPU limit (nếu có)
            if nano_cpus_limit:
                run_kwargs["nano_cpus"] = nano_cpus_limit
                _logger.info(f"[SaaS] Thử tạo Container với CPU limit: {self.plan_id.cpu_limit} core(s)")

            container = client.containers.run(**run_kwargs)

        except Exception as first_error:
            # Nếu lỗi do NanoCPUs không được hỗ trợ → retry không có CPU limit
            if nano_cpus_limit and "NanoCPUs" in str(first_error):
                _logger.warning(
                    f"[SaaS] Kernel không hỗ trợ NanoCPUs, retry không giới hạn CPU..."
                )
                run_kwargs.pop("nano_cpus", None)
                try:
                    container = client.containers.run(**run_kwargs)
                except Exception as retry_error:
                    self.write({"state": "error"})
                    raise UserError(_("Lỗi Docker Engine:\n\n%s") % str(retry_error))
            else:
                self.write({"state": "error"})
                raise UserError(_("Lỗi Docker Engine:\n\n%s") % str(first_error))

        self.write(
            {
                "state": "running",
                "container_id": container.id[:12],
                "db_name": db_name,
                "port": assigned_port,
            }
        )

        self.message_post(
            body=Markup(
                "✅ <b>Khởi tạo THÀNH CÔNG!</b><br/>"
                "🌐 <a href='http://%s' target='_blank'>http://%s</a><br/>"
                "🗄️ DB: %s &nbsp;|&nbsp; 🐳 Container: %s<br/>"
                "📂 Addons: %s"
            )
            % (
                self.tenant_domain,
                self.tenant_domain,
                db_name,
                container.id[:12],
                cons_addons_path,
            )
        )

    def action_stop_tenant(self):
        """Tạm dừng Container."""
        self.ensure_one()
        if not docker or not self.container_id:
            return
        client = docker.from_env()
        try:
            tenant_slug = self._get_tenant_slug()
            containers = client.containers.list(
                all=True, filters={"name": f"odoo_tenant_{tenant_slug}"}
            )
            if containers:
                containers[0].stop()
            self.state = "stopped"
            self.message_post(body=Markup("⏸️ <b>Đã tạm dừng dịch vụ.</b>"))
        except Exception as e:
            raise UserError(_("Lỗi dừng container: %s") % str(e))

    def action_start_tenant(self):
        """Mở lại Container."""
        self.ensure_one()
        if not docker or not self.container_id:
            return
        client = docker.from_env()
        try:
            tenant_slug = self._get_tenant_slug()
            containers = client.containers.list(
                all=True, filters={"name": f"odoo_tenant_{tenant_slug}"}
            )
            if containers:
                containers[0].start()
            self.state = "running"
            self.message_post(body=Markup("▶️ <b>Đã mở lại dịch vụ.</b>"))
        except Exception as e:
            raise UserError(_("Lỗi bật container: %s") % str(e))

    # ========================================================
    #  XEM LOG CONTAINER (Hiển thị trên giao diện)
    # ========================================================
    def action_view_container_logs(self):
        """Đọc Docker container logs và hiển thị trong popup wizard."""
        self.ensure_one()
        if not docker:
            raise UserError(
                _("Server thiếu thư viện 'docker'. Cần: pip install docker")
            )
        if not self.container_id and self.state == "draft":
            raise UserError(_("Tenant chưa được khởi tạo."))

        try:
            client = docker.from_env()
        except Exception as e:
            raise UserError(_("Không thể kết nối Docker Engine: %s") % str(e))

        tenant_slug = self._get_tenant_slug()
        containers = client.containers.list(
            all=True, filters={"name": f"odoo_tenant_{tenant_slug}"}
        )

        if not containers:
            raise UserError(
                _("Không tìm thấy Container cho Tenant '%s'.") % tenant_slug
            )

        container = containers[0]
        try:
            # Lấy 80 dòng log cuối cùng
            log_output = container.logs(tail=80, timestamps=True).decode(
                "utf-8", errors="replace"
            )
        except Exception as e:
            log_output = f"Lỗi khi đọc log: {e}"

        # Kiểm tra trạng thái container
        container_status = container.status  # running, exited, created, etc.
        container_name = container.name

        # Tạo header thông tin
        header = (
            f"═══════════════════════════════════════════════\n"
            f"  📋 DOCKER LOG: {container_name}\n"
            f"  📌 Trạng thái: {container_status.upper()}\n"
            f"  🔗 Container ID: {container.id[:12]}\n"
            f"═══════════════════════════════════════════════\n\n"
        )

        full_log = header + log_output

        # Mở wizard hiển thị log
        return {
            "type": "ir.actions.act_window",
            "name": f"📋 Log Container: {container_name}",
            "res_model": "saas.tenant.log.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_tenant_id": self.id,
                "default_container_name": container_name,
                "default_container_status": container_status,
                "default_log_content": full_log,
            },
        }

    def action_destroy_tenant(self):
        """Xóa hoàn toàn Container, Anonymous Volumes và Database của Tenant."""
        self.ensure_one()
        if not self.env.user.has_group("azhome_saas.group_saas_root"):
            raise UserError(
                _(
                    "Bạn không có quyền Kỹ thuật (Root) để thực hiện thao tác xóa dữ liệu!"
                )
            )

        if not docker:
            raise UserError(
                _("Server thiếu thư viện 'docker'. Cần: pip install docker")
            )

        if self.state == "draft":
            return

        try:
            client = docker.from_env()
        except Exception as e:
            raise UserError(_("Không thể kết nối Docker Engine: %s") % str(e))

        # 1. Xóa Database từ db_master (Brute force ngắt kết nối)
        if self.db_name:
            try:
                # Docker filter chính xác tên container
                db_master_containers = client.containers.list(all=True, filters={"name": "db_master"})
                if db_master_containers:
                    db_master = db_master_containers[0]
                    _logger.info(f"[SaaS] Đang xóa triệt để Database: {self.db_name}")
                    
                    # Lệnh SQL ngắt toàn bộ session đang kết nối vào DB này để tránh lỗi "database is being accessed by other users"
                    terminate_sql = (
                        f"psql -U odoo -d postgres -c \"SELECT pg_terminate_backend(pid) "
                        f"FROM pg_stat_activity WHERE datname = '{self.db_name}' AND pid <> pg_backend_pid();\""
                    )
                    drop_sql = f"dropdb --if-exists -U odoo {self.db_name}"
                    
                    # Thực thi ngắt kết nối
                    db_master.exec_run(terminate_sql)
                    # Thực thi xóa
                    exit_code, output = db_master.exec_run(drop_sql)
                    
                    if exit_code != 0:
                        _logger.warning(f"Lỗi khi dropdb '{self.db_name}': {output}")
                    else:
                        _logger.info(f"Đã xóa thành công Database: {self.db_name}")
                else:
                    _logger.warning("Không thấy container 'db_master' để chạy lệnh xóa DB.")
            except Exception as e:
                _logger.error(f"Ngoại lệ khi xóa DB {self.db_name}: {e}")

        # 2. Xóa Container & dọn rác Volume
        try:
            tenant_slug = self._get_tenant_slug()
            containers = client.containers.list(
                all=True, filters={"name": f"odoo_tenant_{tenant_slug}"}
            )
            for c in containers:
                c.remove(force=True, v=True)
                _logger.info(f"Đã remove force+volume container {c.name}")
        except Exception as e:
            raise UserError(_("Lỗi khi xóa Container:\n\n%s") % str(e))

        # 3. Trả về nháp
        self.write(
            {
                "state": "draft",
                "container_id": False,
                "db_name": False,
                "port": 0,
                "current_users_count": 0,
                "disk_usage_mb": 0.0,
            }
        )
        self.message_post(
            body=Markup(
                "💥 <b>ĐÃ XÓA SẠCH DỮ LIỆU!</b><br/>"
                "Container, Database và các luồng tệp đính kèm đã bị hủy khỏi máy chủ. "
                "Hệ thống trả về trạng thái Nháp."
            )
        )


class SaasTenantUpgradeWizard(models.TransientModel):
    _name = "saas.tenant.upgrade.wizard"
    _description = "Wizard Nâng - Hạ Gói cước"

    tenant_id = fields.Many2one(
        "saas.tenant", string="Khách hàng", required=True, readonly=True
    )
    current_plan_id = fields.Many2one("saas.plan", string="Gói hiện tại", readonly=True)
    new_plan_id = fields.Many2one(
        "saas.plan",
        string="Gói mới",
        required=True,
        domain="[('id', '!=', current_plan_id)]",
    )
    note = fields.Text(string="Ghi chú")

    def action_confirm_upgrade(self):
        """Xác nhận thay đổi gói."""
        self.ensure_one()
        if self.new_plan_id == self.current_plan_id:
            raise UserError(_("Gói mới phải khác gói hiện tại!"))

        tenant = self.tenant_id
        new_plan = self.new_plan_id

        # Kiểm tra giới hạn khi hạ cấp (Downgrade check)
        if new_plan.max_users < tenant.current_users_count:
            raise UserError(
                _(
                    "⚠️ Không thể hạ cấp gói!\n\n"
                    "Số lượng người dùng hiện tại (%s) vượt quá giới hạn của gói mới (%s).\n"
                    "Vui lòng xóa bớt người dùng trên hệ thống của khách hàng trước khi thực hiện."
                )
                % (tenant.current_users_count, new_plan.max_users)
            )

        max_mb_allowed = new_plan.disk_storage * 1024
        if max_mb_allowed < tenant.disk_usage_mb:
            raise UserError(
                _(
                    "⚠️ Không thể hạ cấp gói!\n\n"
                    "Dung lượng dữ liệu hiện tại (%.2f MB) vượt quá giới hạn của gói mới (%s GB).\n"
                    "Vui lòng yêu cầu khách hàng xóa bớt các file đính kèm, hình ảnh lớn trước khi thực hiện."
                )
                % (tenant.disk_usage_mb, new_plan.disk_storage)
            )

        self.tenant_id._apply_plan_upgrade(self.new_plan_id)
        return {"type": "ir.actions.act_window_close"}


class SaasTenantLogWizard(models.TransientModel):
    _name = "saas.tenant.log.wizard"
    _description = "Xem Log Container Tenant"

    tenant_id = fields.Many2one("saas.tenant", string="Tenant", readonly=True)
    container_name = fields.Char(string="Container", readonly=True)
    container_status = fields.Char(string="Trạng thái", readonly=True)
    log_content = fields.Text(string="Nội dung Log", readonly=True)

    def action_refresh_log(self):
        """Làm mới log (gọi lại action từ tenant)."""
        if self.tenant_id:
            return self.tenant_id.action_view_container_logs()
        return {"type": "ir.actions.act_window_close"}
