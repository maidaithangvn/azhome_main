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


class AzHomeSaasManualController(http.Controller):

    @http.route('/azhome_saas/manual', type='http', auth='user', website=True)
    def show_saas_manual(self, **kw):
        """Render the SaaS deployment manual QWeb template."""
        return request.render('azhome_saas.azhome_saas_manual_template', {})
