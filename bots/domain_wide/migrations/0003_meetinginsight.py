# Generated manually

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('domain_wide', '0002_pipeline_activity'),
        ('bots', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='MeetingInsight',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('supabase_meeting_id', models.CharField(blank=True, db_index=True, default='', max_length=64)),
                ('summary', models.TextField(blank=True, default='')),
                ('action_items', models.JSONField(default=list)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('recording', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='insight', to='bots.recording')),
                ('bot', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='insights', to='bots.bot')),
            ],
            options={
                'app_label': 'domain_wide',
            },
        ),
    ]
