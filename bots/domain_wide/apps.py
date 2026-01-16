from django.apps import AppConfig


class DomainWideConfig(AppConfig):
    name = 'bots.domain_wide'
    verbose_name = 'Domain Wide Calendar Integration'

    def ready(self):
        # Import signals to register them
        import bots.domain_wide.signals  # noqa
