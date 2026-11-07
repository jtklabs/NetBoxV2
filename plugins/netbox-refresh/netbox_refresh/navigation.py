from netbox.plugins import PluginMenu, PluginMenuButton, PluginMenuItem


def _item(name, label, permission_model, *, add=True, import_=True):
    buttons = []
    if add:
        buttons.append(PluginMenuButton(
            link='plugins:netbox_refresh:%s_add' % name,
            title='Add',
            icon_class='mdi mdi-plus-thick',
            permissions=['netbox_refresh.add_%s' % permission_model],
        ))
    if import_:
        buttons.append(PluginMenuButton(
            link='plugins:netbox_refresh:%s_bulk_import' % name,
            title='Import',
            icon_class='mdi mdi-upload',
            permissions=['netbox_refresh.add_%s' % permission_model],
        ))
    return PluginMenuItem(
        link='plugins:netbox_refresh:%s_list' % name,
        link_text=label,
        permissions=['netbox_refresh.view_%s' % permission_model],
        buttons=tuple(buttons),
    )


lifecycle = _item('modellifecycle', 'Model Lifecycles', 'modellifecycle')
replacement_prices = _item('replacementprice', 'Replacement Prices', 'replacementprice')

refresh_report = PluginMenuItem(
    link='plugins:netbox_refresh:refresh_report',
    link_text='Refresh Report',
    permissions=['netbox_refresh.view_modellifecycle'],
)

# Standards have no CSV importer: the approved-versions many-to-many does not
# round-trip through a flat CSV in any way that would not mislead.
software_versions = _item('softwareversion', 'Software Versions', 'softwareversion')
software_standards = _item(
    'softwarestandard', 'Software Standards', 'softwarestandard', import_=False
)
device_software = _item('devicesoftware', 'Device Software', 'devicesoftware')

compliance_report = PluginMenuItem(
    link='plugins:netbox_refresh:compliance_report',
    link_text='Compliance Report',
    permissions=['netbox_refresh.view_devicesoftware'],
)

version_rollup = PluginMenuItem(
    link='plugins:netbox_refresh:version_rollup',
    link_text='Version Rollup',
    permissions=['netbox_refresh.view_devicesoftware'],
)

menu = PluginMenu(
    label='Lifecycle',
    groups=(
        ('Coverage', (
            _item('licenseentitlement', 'License Entitlements', 'licenseentitlement', import_=False),
            _item('licenseassignment', 'License Assignments', 'licenseassignment', import_=False),
            _item('supportcontract', 'Support Contracts', 'supportcontract', import_=False),
            PluginMenuItem(
                link='plugins:netbox_refresh:coverage_report',
                link_text='Coverage Report',
                permissions=['dcim.view_device', 'netbox_refresh.view_supportcontract'],
            ),
        )),
        ('Hardware', (lifecycle, replacement_prices, refresh_report)),
        ('Software', (software_versions, software_standards, device_software,
                      compliance_report, version_rollup)),
    ),
    icon_class='mdi mdi-calendar-clock',
)
