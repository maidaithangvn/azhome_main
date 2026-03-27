{
    'name': 'AZHOME SaaS Multi-Tenant Controller',
    'version': '1.0',
    'category': 'Construction',
    'summary': 'Quản lý Khách hàng SaaS qua Docker Container',
    'description': """
    Quản lý việc tự động sinh Container Odoo cho khách hàng SaaS.
    Sử dụng Traefik làm proxy ngược và thư viện docker-py.
    """,
    'author': 'AZHOME',
    'depends': ['base', 'mail'],
    'data': [
        'security/groups.xml',
        'security/ir.model.access.csv',
        'data/saas_plan_data.xml',
        'data/ir_cron.xml',
        'views/saas_plan_views.xml',
        'views/saas_tenant_views.xml',
        'views/az_saas_manual_views.xml',
        'views/menu_views.xml',
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
    'license': 'LGPL-3',
}
