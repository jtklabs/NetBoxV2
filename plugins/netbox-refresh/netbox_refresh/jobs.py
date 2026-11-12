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


class LifecycleActionRefreshJob(JobRunner):
    class Meta:
        name = 'Lifecycle actions and renewal reminders'

    def run(self, *args, **kwargs):
        from netbox.context import events_queue
        from extras.events import flush_events
        from netbox_refresh.operations import refresh_actions
        token = events_queue.set({})
        try:
            summary = refresh_actions(emit_events=True)
            if events := list(events_queue.get().values()):
                flush_events(events)
        finally:
            events_queue.reset(token)
        self.job.data = summary
        self.job.save(update_fields=['data'])
        return summary
