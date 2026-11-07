from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('extras', '0140_imageattachment_image_size'),
        ('netbox_refresh', '0008_license_coverage'),
    ]

    operations = [
        migrations.AddField(
            model_name='softwarestandard',
            name='device_tags',
            field=models.ManyToManyField(
                blank=True,
                help_text='Devices carrying any of these tags use this standard ahead of type or platform standards.',
                related_name='scoped_software_standards',
                to='extras.tag',
            ),
        ),
    ]
