from django.core.management.base import BaseCommand

from netbox_refresh.cisco import CiscoEoxClient, CiscoEoxError
from netbox_refresh.sync import get_credentials, sync


class Command(BaseCommand):
    help = 'Populate hardware lifecycle records from the Cisco EoX API.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='report what would change without writing')
        parser.add_argument('--force', action='store_true',
                            help='also overwrite records marked as manually maintained')
        parser.add_argument('--limit', type=int,
                            help='only process the first N device/module types')
        parser.add_argument('--check-auth', action='store_true',
                            help='only obtain an API token with the configured '
                                 'credentials and report; looks nothing up')

    def handle(self, *args, **options):
        if options['check_auth']:
            return self._check_auth()
        try:
            summary = sync(
                dry_run=options['dry_run'],
                force=options['force'],
                limit=options.get('limit'),
                logger_fn=lambda msg: self.stdout.write('  %s' % msg),
            )
        except CiscoEoxError as exc:
            self.stderr.write(self.style.ERROR(str(exc)))
            return

        self.stdout.write('')
        for key in ('types', 'pids', 'created', 'updated', 'no_data', 'not_announced',
                    'skipped_manual', 'replacements_linked', 'errors'):
            self.stdout.write('  %-20s %s' % (key, summary.get(key, 0)))
        if options['dry_run']:
            self.stdout.write(self.style.WARNING('dry run — nothing was written'))

    def _check_auth(self):
        """Prove the credentials work, and nothing more.

        Prints which client id was used (masked) so a wrong-environment
        mistake is visible, and how long the token is good for. The secret is
        never printed. Exit status says pass or fail for scripts.
        """
        client_id, client_secret = get_credentials()
        shown = (client_id[:4] + '…' + client_id[-4:]) if len(client_id) > 8 else client_id
        if not client_id or not client_secret:
            self.stderr.write(self.style.ERROR(
                'No Cisco credentials configured (CISCO_CLIENT_ID / CISCO_CLIENT_SECRET).'))
            raise SystemExit(1)
        try:
            seconds = CiscoEoxClient(client_id, client_secret).check_auth()
        except CiscoEoxError as exc:
            self.stderr.write(self.style.ERROR('client_id %s: %s' % (shown, exc)))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS(
            'client_id %s authenticated; token valid for about %d minutes.'
            % (shown, seconds // 60)))
