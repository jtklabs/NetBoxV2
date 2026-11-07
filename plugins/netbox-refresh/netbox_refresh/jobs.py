from netbox.jobs import JobRunner

from netbox_refresh.sync import sync


class CiscoEoxSyncJob(JobRunner):
    """Schedulable Cisco EoX sync — Operations > Jobs in the NetBox UI."""

    class Meta:
        name = 'Cisco EoX lifecycle sync'

    def run(self, *args, **kwargs):
        summary = sync(
            dry_run=kwargs.get('dry_run', False),
            force=kwargs.get('force', False),
            logger_fn=lambda msg: self.logger.info(msg),
        )
        return summary
