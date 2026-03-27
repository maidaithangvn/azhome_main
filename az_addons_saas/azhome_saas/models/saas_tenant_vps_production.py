import logging
from odoo import models, fields, api, _
from odoo.exceptions import UserError
from markupsafe import Markup

_logger = logging.getLogger(__name__)

try:
    import docker
except ImportError:
    docker = None
    _logger.warning("Thư viện 'docker' (docker-py) chưa được cài đặt. Vui lòng chạy: pip install docker-py")


class SaasTenant(models.Model):
    _name = 'saas.tenant'
    _description = 'Khách hàng SaaS'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    partner_id = fields.Many2one('res.partner', string='Khách hàng (Odoo Contact)', required=True, tracking=True)
    name = fields.Char(related='partner_id.name', string='Tên Công ty/Khách hàng', store=True)
    domain_prefix = fields.Char(string='Prefix Domain', required=True, tracking=True)
    email = fields.Char(related='partner_id.email', string='Email Liên hệ', readonly=False)
    phone = fields.Char(related='partner_id.phone', string='Điện thoại', readonly=False)

    user_id = fields.Many2one(
        'res.users', string='Nhân viên phụ trách',
        default=lambda self: self.env.user, tracking=True,
    )

    tenant_domain = fields.Char(string='Domain đầy đủ', compute='_compute_domain', store=True)

    db_name = fields.Char(string='Tên Database', readonly=True)
    container_id = fields.Char(string='Docker Container ID', readonly=True)
    port = fields.Integer(string='Cổng TCP (Local)', readonly=True)

    # === THỐNG KÊ TÀI NGUYÊN ===
    current_users_count = fields.Integer(string='Số User đang dùng', default=0, readonly=True)
    max_users_display = fields.Integer(string='Giới hạn User', related='plan_id.max_users', readonly=True)
    disk_usage_mb = fields.Float(string='Dung lượng đã dùng (MB)', default=0, readonly=True)
    max_disk_display = fields.Integer(string='Hạn mức Lưu trữ (GB)', related='plan_id.disk_storage', readonly=True)
    stats_last_updated = fields.Datetime(string='Cập nhật lần cuối', readonly=True)

    plan_id = fields.Many2one('saas.plan', string='Khách Mua Gói', required=True, tracking=True)
    expiry_date = fields.Date(string='Ngày hết hạn', tracking=True)

    state = fields.Selection([
        ('draft', 'Mới'),
        ('running', 'Đang hoạt động'),
        ('stopped', 'Tạm dừng'),
        ('error', 'Lỗi khởi tạo')
    ], string='Trạng thái', default='draft', tracking=True)

    setup_modules = fields.Char(
        string='Mô-đun Khởi tạo',
        default='base,mail,hr,project,sale_management,az_construction_management',
    )

    @api.onchange('plan_id')
    def _onchange_plan_id(self):
        if self.plan_id and self.plan_id.duration_days > 0:
            from datetime import date, timedelta
            self.expiry_date = date.today() + timedelta(days=self.plan_id.duration_days)

    @api.depends('domain_prefix')
    def _compute_domain(self):
        for rec in self:
            if rec.domain_prefix:
                rec.tenant_domain = f"{rec.domain_prefix.lower().strip()}.azhome.top"
            else:
                rec.tenant_domain = False

    @api.model
    def _cron_check_tenant_expiry(self):
        """Cronjob quét tenant sắp hết hạn (15 ngày)."""
        from datetime import date, timedelta
        target_date = date.today() + timedelta(days=15)
        tenants_expiring = self.search([
            ('state', 'in', ['running', 'stopped']),
            ('expiry_date', '=', target_date)
        ])
        for tenant in tenants_expiring:
            responsible_user_id = tenant.user_id.id or self.env.user.id
            tenant.activity_schedule(
                'mail.mail_activity_data_todo',
                summary=f'Sắp hết hạn Gói SaaS: {tenant.name}',
                note=f"<p>Khách hàng <b>{tenant.name}</b> hết hạn gói <b>{tenant.plan_id.name}</b> vào <b>{tenant.expiry_date}</b>.</p>",
                user_id=responsible_user_id,
                date_deadline=tenant.expiry_date
            )
            tenant.message_post(body=Markup(f"⏰ <b>Nhắc nhở:</b> Gói thuê bao còn 15 ngày."))

    _sql_constraints = [
        ('domain_prefix_unique', 'unique(domain_prefix)', 'Prefix này đã tồn tại!')
    ]

    def unlink(self):
        """Ngăn chặn việc vô tình xóa bản ghi trên giao diện List/Form khi Tenant đang chạy."""
        for rec in self:
            if rec.state == 'running':
                raise UserError(_(
                    "Không thể xóa Khách hàng đang ở trạng thái 'Đang hoạt động'.\n"
                    "Vui lòng chạy chức năng [Hủy Dữ liệu] để dọn sạch Container/Database trên máy chủ trước, "
                    "hệ thống sẽ trả về trạng thái Nháp rồi bạn mới có thể xóa sổ bản ghi này."
                ))
        return super(SaasTenant, self).unlink()

    # ========================================================
    #  CẬP NHẬT THỐNG KÊ (Master pull từ Container qua HTTP)
    # ========================================================
    def action_refresh_stats(self):
        """Master chủ động query Tenant Odoo qua HTTP JSON-RPC để lấy số user & dung lượng."""
        self.ensure_one()
        if self.state != 'running' or not self.port:
            raise UserError(_("Tenant chưa hoạt động hoặc chưa có cổng kết nối."))

        import requests as req
        
        tenant_slug = self.domain_prefix.lower().strip().replace(" ", "")
        container_name = f"odoo_tenant_{tenant_slug}_{self.port}"
        
        # 1. Thử qua Localhost (dành cho Windows Host test)
        # 2. Thử qua Container DNS nội bộ (dành cho VPS hoặc NAS chạy master trong Docker)
        urls_to_try = [
            f"http://localhost:{self.port}/azhome/tenant_stats",
            f"http://{container_name}:8069/azhome/tenant_stats",
        ]

        payload = {
            'jsonrpc': '2.0',
            'method': 'call',
            'params': {
                'token': self.id, # Dùng ID tenant làm token bảo mật đơn giản
            }
        }

        resp = None
        for url in urls_to_try:
            try:
                resp = req.post(url, json=payload, timeout=8)
                break
            except req.exceptions.ConnectionError:
                continue
                
        if not resp:
            raise UserError(_(
                "Không thể kết nối đến Tenant tại cổng %s hoặc hostname nội bộ '%s'.\n"
                "Đảm bảo Container đang chạy và chung mạng az_saas_network (hoặc map port ra host)."
            ) % (self.port, container_name))

        try:
            result = resp.json()

            if 'error' in result:
                raise UserError(_("Tenant trả về lỗi: %s") % result['error'].get('message'))

            data = result.get('result', {})
            if data.get('status') == 'error':
                raise UserError(_("Lỗi từ Tenant: %s") % data.get('message'))

            stats = data.get('stats', {})
            user_count = stats.get('user_count', 0)
            disk_mb = stats.get('disk_usage_mb', 0)

            self.write({
                'current_users_count': user_count,
                'disk_usage_mb': disk_mb,
                'stats_last_updated': fields.Datetime.now(),
            })

            self.message_post(body=Markup(
                "📊 <b>Cập nhật thống kê (Pull)!</b><br/>"
                "👤 User: <b>%s</b> / %s &nbsp;|&nbsp; 💾 Disk: <b>%s MB</b>"
            ) % (user_count, self.plan_id.max_users, disk_mb))

        except Exception as e:
            raise UserError(_("Lỗi khi xử lý dữ liệu thống kê: %s") % str(e))

    # ========================================================
    #  GIA HẠN GÓI CƯỚC
    # ========================================================
    def action_renew_subscription(self):
        """Gia hạn thêm N ngày theo gói cước hiện tại."""
        self.ensure_one()
        if not self.plan_id or not self.plan_id.duration_days:
            raise UserError(_("Gói cước chưa có thời hạn. Vui lòng kiểm tra cấu hình Gói."))

        from datetime import timedelta
        base_date = self.expiry_date or fields.Date.today()
        new_expiry = base_date + timedelta(days=self.plan_id.duration_days)
        old_expiry = self.expiry_date

        self.write({'expiry_date': new_expiry})

        self._push_env_to_container({'AZHOME_EXPIRY_DATE': str(new_expiry)})

        self.message_post(body=Markup(
            "🔄 <b>GIA HẠN thành công!</b><br/>"
            "Gói: <b>%s</b> &nbsp;|&nbsp; Hạn cũ: %s → Hạn mới: <b>%s</b> (+%s ngày)"
        ) % (self.plan_id.name, old_expiry or 'N/A', new_expiry, self.plan_id.duration_days))

    # ========================================================
    #  NÂNG CẤP GÓI CƯỚC (Wizard)
    # ========================================================
    def action_upgrade_plan(self):
        """Mở wizard để chọn gói mới."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Nâng - Hạ / Đổi Gói cước',
            'res_model': 'saas.tenant.upgrade.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_tenant_id': self.id,
                'default_current_plan_id': self.plan_id.id,
            },
        }

    def _apply_plan_upgrade(self, new_plan):
        """Áp dụng gói mới lên Tenant."""
        self.ensure_one()
        old_plan = self.plan_id
        from datetime import timedelta
        new_expiry = fields.Date.today() + timedelta(days=new_plan.duration_days)

        self.write({
            'plan_id': new_plan.id,
            'expiry_date': new_expiry,
        })

        self._push_env_to_container({
            'AZHOME_MAX_USERS': str(new_plan.max_users),
            'AZHOME_PLAN_CODE': str(new_plan.code),
            'AZHOME_EXPIRY_DATE': str(new_expiry),
        })

        self.message_post(body=Markup(
            "⬆️⬇️ <b>THAY ĐỔI GÓI!</b><br/>"
            "Cũ: %s (%s users) → Mới: <b>%s</b> (<b>%s</b> users)<br/>"
            "Hết hạn mới: <b>%s</b>"
        ) % (old_plan.name, old_plan.max_users, new_plan.name, new_plan.max_users, new_expiry))

    def _push_env_to_container(self, env_updates):
        """Ghi biến môi trường mới vào Container qua docker exec."""
        if not docker or not self.container_id:
            return
        try:
            client = docker.from_env()
            tenant_slug = self.domain_prefix.lower().strip()
            containers = client.containers.list(filters={'name': f'odoo_tenant_{tenant_slug}'})
            if not containers:
                return
            env_lines = '\\n'.join([f"{k}={v}" for k, v in env_updates.items()])
            containers[0].exec_run(f'sh -c "echo -e \'{env_lines}\' > /etc/azhome_env"')
            _logger.info(f"[SaaS] Đã push ENV vào Container: {list(env_updates.keys())}")
        except Exception as e:
            _logger.warning(f"[SaaS] Lỗi push ENV: {e}")

    # ========================================================
    #  PROVISION & LIFECYCLE
    # ========================================================
    def action_provision_tenant(self):
        """Tạo Odoo Container mới qua Docker Engine API."""
        self.ensure_one()
        if not docker:
            raise UserError(_("Server thiếu thư viện 'docker'. Cần: pip install docker"))
        if self.state not in ['draft', 'error']:
            raise UserError(_("Tiến trình khởi tạo đã được thực thi."))

        try:
            client = docker.from_env()
        except Exception as e:
            raise UserError(_("Không thể kết nối Docker: %s") % str(e))

        tenant_slug = self.domain_prefix.lower().strip().replace(" ", "")
        db_name = f"azhome_tenant_{tenant_slug}"
        db_password = "odoo_saas_super_secret"

        labels = {
            "traefik.enable": "true", "saas": "true",
            f"traefik.http.routers.{tenant_slug}.rule": f"Host(`{self.tenant_domain}`)",
            f"traefik.http.services.{tenant_slug}.loadbalancer.server.port": "8069",
        }

        import random
        assigned_port = random.randint(10000, 20000)

        env_vars = {
            "HOST": "db_master", "USER": "odoo", "PASSWORD": db_password,
            "ODOO_MAX_CRON_THREADS": "1",
            "AZHOME_MAX_USERS": str(self.plan_id.max_users),
            "AZHOME_PLAN_CODE": str(self.plan_id.code),
            "AZHOME_TENANT_ID": str(self.id),
            "AZHOME_EXPIRY_DATE": str(self.expiry_date),
            "AZHOME_MASTER_URL": self.env['ir.config_parameter'].sudo().get_param(
                'web.base.url', 'http://host.docker.internal:8069'),
        }

        tenant_addons_path = self.env['ir.config_parameter'].sudo().get_param(
            'azhome_saas.tenant_addons_path',
            '/volume1/docker/azhome_saas/addons'
        )
        docker_mounts = {tenant_addons_path: {'bind': '/mnt/extra-addons', 'mode': 'ro'}}


        cmd_args = (
            f"odoo -d {db_name} -i {self.setup_modules} "
            f"--load-language=vi_VN --db-filter=^{db_name}$ "
            f"--no-database-list --without-demo=all"
        )

        mem_limit_bytes = self.plan_id.mem_limit or "1024m"
        nano_cpus_limit = int(self.plan_id.cpu_limit) * 1000000000 if self.plan_id.cpu_limit else 1000000000

        try:
            container = client.containers.run(
                image="odoo:19",
                name=f"odoo_tenant_{tenant_slug}_{assigned_port}",
                environment=env_vars, command=cmd_args,
                volumes=docker_mounts, labels=labels,
                ports={'8069/tcp': assigned_port},
                network="az_saas_network", detach=True,
                mem_limit=mem_limit_bytes, nano_cpus=nano_cpus_limit,
                restart_policy={"Name": "unless-stopped"}
            )

            self.write({
                'state': 'running', 'container_id': container.id[:12],
                'db_name': db_name, 'port': assigned_port,
            })

            self.message_post(body=Markup(
                "✅ <b>Khởi tạo THÀNH CÔNG!</b><br/>"
                "🌐 <a href='http://%s' target='_blank'>http://%s</a><br/>"
                "🗄️ DB: %s &nbsp;|&nbsp; 🐳 Container: %s"
            ) % (self.tenant_domain, self.tenant_domain, db_name, container.id[:12]))

        except Exception as e:
            self.write({'state': 'error'})
            raise UserError(_("Lỗi Docker Engine:\n\n%s") % str(e))

    def action_stop_tenant(self):
        """Tạm dừng Container."""
        self.ensure_one()
        if not docker or not self.container_id:
            return
        client = docker.from_env()
        try:
            tenant_slug = self.domain_prefix.lower().strip()
            containers = client.containers.list(all=True, filters={'name': f'odoo_tenant_{tenant_slug}'})
            if containers:
                containers[0].stop()
            self.state = 'stopped'
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
            tenant_slug = self.domain_prefix.lower().strip()
            containers = client.containers.list(all=True, filters={'name': f'odoo_tenant_{tenant_slug}'})
            if containers:
                containers[0].start()
            self.state = 'running'
            self.message_post(body=Markup("▶️ <b>Đã mở lại dịch vụ.</b>"))
        except Exception as e:
            raise UserError(_("Lỗi bật container: %s") % str(e))

    def action_destroy_tenant(self):
        """Xóa hoàn toàn Container, Anonymous Volumes và Database của Tenant."""
        self.ensure_one()
        if not self.env.user.has_group('azhome_saas.group_saas_root'):
            raise UserError(_("Bạn không có quyền Kỹ thuật (Root) để thực hiện thao tác xóa dữ liệu!"))
            
        if not docker:
            raise UserError(_("Server thiếu thư viện 'docker'. Cần: pip install docker"))

        if self.state == 'draft':
            return  # Không cần xóa nếu chưa tạo

        try:
            client = docker.from_env()
        except Exception as e:
            raise UserError(_("Không thể kết nối Docker Engine: %s") % str(e))

        # 1. Xóa Database từ db_master
        if self.db_name:
            try:
                db_master_containers = client.containers.list(filters={'name': 'db_master'})
                if db_master_containers:
                    db_master = db_master_containers[0]
                    # Thực thi lệnh dropdb với User odoo
                    exit_code, output = db_master.exec_run(f'dropdb --if-exists -U odoo {self.db_name}')
                    if exit_code != 0:
                        _logger.warning(f"Lỗi trả về khi dropdb '{self.db_name}': {output}")
                else:
                    _logger.warning("Không thấy container 'db_master' để chạy lệnh dropdb.")
            except Exception as e:
                # Không break quá trình nếu lỗi, để vẫn xóa được container
                _logger.error(f"Ngoại lệ khi xóa DB {self.db_name}: {e}")

        # 2. Xóa Container & dọn rác Volume (v=True là cốt yếu để xoá Anonymous Volume /var/lib/odoo)
        try:
            tenant_slug = self.domain_prefix.lower().strip().replace(" ", "")
            containers = client.containers.list(all=True, filters={'name': f'odoo_tenant_{tenant_slug}'})
            for c in containers:
                c.remove(force=True, v=True)
                _logger.info(f"Đã remove force+volume container {c.name}")
        except Exception as e:
            raise UserError(_("Lỗi khi xóa Container:\n\n%s") % str(e))

        # 3. Trả về nháp
        self.write({
            'state': 'draft',
            'container_id': False,
            'db_name': False,
            'port': 0,
            'current_users_count': 0,
            'disk_usage_mb': 0.0,
        })
        self.message_post(body=Markup("💥 <b>ĐÃ XÓA SẠCH DỮ LIỆU!</b><br/>"
                                      "Container, Database và các luồng tệp đính kèm đã bị hủy khỏi máy chủ. Hệ thống trả về trạng thái Nháp."))


class SaasTenantUpgradeWizard(models.TransientModel):
    _name = 'saas.tenant.upgrade.wizard'
    _description = 'Wizard Nâng - Hạ Gói cước'

    tenant_id = fields.Many2one('saas.tenant', string='Khách hàng', required=True, readonly=True)
    current_plan_id = fields.Many2one('saas.plan', string='Gói hiện tại', readonly=True)
    new_plan_id = fields.Many2one('saas.plan', string='Gói mới', required=True,
                                   domain="[('id', '!=', current_plan_id)]")
    note = fields.Text(string='Ghi chú')

    def action_confirm_upgrade(self):
        """Xác nhận thay đổi gói."""
        self.ensure_one()
        if self.new_plan_id == self.current_plan_id:
            raise UserError(_("Gói mới phải khác gói hiện tại!"))

        tenant = self.tenant_id
        new_plan = self.new_plan_id

        # Kiểm tra giới hạn khi hạ cấp (Downgrade check)
        # 1. Kiểm tra số lượng người dùng
        if new_plan.max_users < tenant.current_users_count:
            raise UserError(_(
                "⚠️ Không thể hạ cấp gói!\n\n"
                "Số lượng người dùng hiện tại (%s) vượt quá giới hạn của gói mới (%s).\n"
                "Vui lòng xóa bớt người dùng trên hệ thống của khách hàng trước khi thực hiện."
            ) % (tenant.current_users_count, new_plan.max_users))

        # 2. Kiểm tra dung lượng ổ cứng (GB vs MB)
        max_mb_allowed = new_plan.disk_storage * 1024
        if max_mb_allowed < tenant.disk_usage_mb:
            raise UserError(_(
                "⚠️ Không thể hạ cấp gói!\n\n"
                "Dung lượng dữ liệu hiện tại (%.2f MB) vượt quá giới hạn của gói mới (%s GB).\n"
                "Vui lòng yêu cầu khách hàng xóa bớt các file đính kèm, hình ảnh lớn trước khi thực hiện."
            ) % (tenant.disk_usage_mb, new_plan.disk_storage))

        self.tenant_id._apply_plan_upgrade(self.new_plan_id)
        return {'type': 'ir.actions.act_window_close'}

