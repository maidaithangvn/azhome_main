# -*- coding: utf-8 -*-
from odoo import http
from odoo.http import request
import logging

_logger = logging.getLogger(__name__)

class SaasStatsController(http.Controller):

    @http.route('/saas/update_stats', type='json', auth='none', methods=['POST'], csrf=False)
    def update_stats(self, **post):
        """
        Endpoint để máy con gửi số lượng user về cho máy Master.
        Tham số: tenant_id, user_count, secret_token (tương lai)
        """
        tenant_id = post.get('tenant_id')
        user_count = post.get('user_count')
        disk_usage = post.get('disk_usage_mb', 0)
        
        if not tenant_id:
            return {'status': 'error', 'message': 'Missing tenant_id'}
        
        _logger.info(f"[SaaS Master] Nhận cập nhật từ Tenant ID {tenant_id}: {user_count} users, {disk_usage} MB disk")
        
        # Cập nhật số lượng user và dung lượng vào bản ghi tương ứng
        tenant = request.env['saas.tenant'].sudo().browse(int(tenant_id))
        if tenant.exists():
            vals = {'current_users_count': int(user_count)}
            if disk_usage:
                vals['disk_usage_mb'] = float(disk_usage)
            tenant.sudo().write(vals)
            return {'status': 'success'}
        
        return {'status': 'error', 'message': 'Tenant not found'}

    @http.route('/azhome/tenant_stats', type='json', auth='none', methods=['POST'], csrf=False)
    def tenant_stats(self, **post):
        """
        Endpoint trên máy con (Tenant) để máy Master gọi vào lấy thông tin.
        """
        # Đếm số lượng user thực tế (Active và không phải Portal/Public)
        user_count = request.env['res.users'].sudo().search_count([
            ('active', '=', True), 
            ('share', '=', False)
        ])
        
        # Lấy dung lượng cơ sở dữ liệu (MB)
        try:
            request.env.cr.execute("SELECT pg_database_size(current_database())")
            db_size = request.env.cr.fetchone()[0]
            db_size_mb = round(db_size / (1024 * 1024), 2)
        except Exception:
            db_size_mb = 0
            
        return {
            'status': 'success',
            'stats': {
                'user_count': user_count,
                'disk_usage_mb': db_size_mb
            }
        }


class AzHomeSaasManualController(http.Controller):

    @http.route('/azhome_saas/manual', type='http', auth='user', website=True)
    def show_saas_manual(self, **kw):
        """Render the SaaS deployment manual QWeb template."""
        return request.render('azhome_saas.azhome_saas_manual_template', {})
