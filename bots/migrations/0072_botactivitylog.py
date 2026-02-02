# Generated manually for BotActivityLog model

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('bots', '0071_add_calendar_auth_type'),
    ]

    operations = [
        migrations.CreateModel(
            name='BotActivityLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('activity_type', models.IntegerField(choices=[
                    (1, 'Navigated to meeting URL'),
                    (2, 'Meeting page loaded'),
                    (3, 'Found join button'),
                    (4, 'Clicked join button'),
                    (5, 'Asking to be let in (waiting room)'),
                    (6, 'Waiting for host to start meeting'),
                    (7, 'Admitted to meeting'),
                    (8, 'Captions enabled'),
                    (10, 'Detected: Request denied'),
                    (11, 'Detected: No one responded'),
                    (12, 'Detected: Meeting not found'),
                    (13, 'Detected: Login required'),
                    (14, 'Detected: Blocked by Google'),
                    (15, 'Detected: Captcha challenge'),
                    (20, 'Login flow started'),
                    (21, 'Login flow completed'),
                    (22, 'Layout configured'),
                    (23, 'Screenshot captured'),
                    (99, 'UI error occurred'),
                ])),
                ('message', models.CharField(blank=True, max_length=500)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('elapsed_ms', models.IntegerField(blank=True, help_text='Milliseconds since bot staged', null=True)),
                ('bot', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='activity_logs', to='bots.bot')),
            ],
            options={
                'ordering': ['created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='botactivitylog',
            index=models.Index(fields=['bot', 'created_at'], name='bots_botact_bot_id_d3b5e5_idx'),
        ),
        migrations.AddIndex(
            model_name='botactivitylog',
            index=models.Index(fields=['activity_type', 'created_at'], name='bots_botact_activit_f8e3a2_idx'),
        ),
    ]
