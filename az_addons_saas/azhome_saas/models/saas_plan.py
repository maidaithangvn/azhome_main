from odoo import models, fields, api

class SaasPlan(models.Model):
    _name = 'saas.plan'
    _description = 'Gói Cước Dịch vụ SaaS AZHOME'

    name = fields.Char(string='Tên gói', required=True)
    code = fields.Char(string='Mã định dạng (Code)', help='VD: FREE, BASIC, PRO, ENTERPRISE', required=True)
    
    max_users = fields.Integer(string='Giới hạn User (Watchdog)', required=True, default=5,
                               help='Biến AZHOME_MAX_USERS sẽ chặn tạo mới nếu vượt số này.')
    cpu_limit = fields.Selection([
        ('1', '1 Core CPU'),
        ('2', '2 Cores CPU'),
        ('4', '4 Cores CPU'),
        ('8', '8 Cores CPU')
    ], string='Cấp CPU', default='1')
    
    mem_limit = fields.Selection([
        ('512m', '512 MB'),
        ('1024m', '1 GB'),
        ('2048m', '2 GB'),
        ('4096m', '4 GB')
    ], string='Cấp RAM Máy ảo', default='1024m')
    
    disk_storage = fields.Integer(string='Lưu trữ (GB)', default=25, help='Lưu lượng ổ cứng SSD được phép xài.')
    
    # Trường thực tế lưu trong DB (số ngày)
    duration_days = fields.Integer(string='Thời gian xài (Ngày)', default=30, help='Hạn sử dụng gói tính theo ngày')
    
    # Các trường helper để nhập liệu nhanh
    duration_value = fields.Integer(string='Thời gian', compute='_compute_duration_helper', inverse='_inverse_duration_helper', store=True)
    duration_unit = fields.Selection([
        ('days', 'Ngày'),
        ('months', 'Tháng'),
        ('years', 'Năm')
    ], string='Đơn vị', compute='_compute_duration_helper', inverse='_inverse_duration_helper', default='months', store=True)
    
    @api.depends('duration_days')
    def _compute_duration_helper(self):
        for rec in self:
            if rec.duration_days > 0 and rec.duration_days % 365 == 0:
                rec.duration_unit = 'years'
                rec.duration_value = rec.duration_days // 365
            elif rec.duration_days > 0 and rec.duration_days % 30 == 0:
                rec.duration_unit = 'months'
                rec.duration_value = rec.duration_days // 30
            else:
                rec.duration_unit = 'days'
                rec.duration_value = rec.duration_days

    def _inverse_duration_helper(self):
        for rec in self:
            if rec.duration_unit == 'days':
                rec.duration_days = rec.duration_value
            elif rec.duration_unit == 'months':
                rec.duration_days = rec.duration_value * 30
            elif rec.duration_unit == 'years':
                rec.duration_days = rec.duration_value * 365

    currency_id = fields.Many2one('res.currency', string='Tiền tệ', default=lambda self: self.env.ref('base.VND', raise_if_not_found=False))

    price_monthly = fields.Float(string='Giá hàng tháng')
    price_yearly = fields.Float(string='Giá hàng năm')
    
    sequence = fields.Integer('Thứ tự', default=10)
    active = fields.Boolean(default=True)

