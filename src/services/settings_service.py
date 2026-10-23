"""Persisted runtime settings (stored as JSON values in ``app_settings``)."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from config import AppConfig
from src.core.settings_rules import SETTING_KEYS, RuntimeSettings, validate_settings_changes
from src.models.orm import AppSetting
from src.models.schemas import Severity, ValidationFailed
from src.utils.time_utils import utc_now


def defaults_from_config(config: AppConfig) -> RuntimeSettings:
    return RuntimeSettings(
        freshness_seconds=config.default_freshness_seconds, timezone=config.default_timezone
    )


class SettingsService:
    def __init__(
        self,
        session: Session,
        defaults: RuntimeSettings | None = None,
        clock: Callable[[], datetime] = utc_now,
    ):
        self.session = session
        self.defaults = defaults or RuntimeSettings()
        self.clock = clock

    def load(self) -> RuntimeSettings:
        """Defaults overlaid with stored values. Corrupt stored values fall back to defaults."""
        stored: dict[str, Any] = {}
        for row in self.session.scalars(select(AppSetting)):
            if row.key in SETTING_KEYS:
                try:
                    stored[row.key] = json.loads(row.value)
                except ValueError:
                    continue
        settings, issues = validate_settings_changes(stored, self.defaults)
        if issues:  # ignore only the invalid keys, keep the valid ones
            bad = {i.field for i in issues}
            settings, _ = validate_settings_changes(
                {k: v for k, v in stored.items() if k not in bad}, self.defaults
            )
        return settings

    def update(self, changes: Mapping[str, Any]) -> RuntimeSettings:
        current = self.load()
        new, issues = validate_settings_changes(changes, current)
        if issues:
            raise ValidationFailed(issues)
        now = self.clock()
        for key in changes:
            value = getattr(new, key)
            if key == "severities":
                value = {k: Severity(v).value for k, v in value.items()}
            row = self.session.get(AppSetting, key)
            text = json.dumps(value)
            if row is None:
                self.session.add(AppSetting(key=key, value=text, updated_at=now))
            else:
                row.value = text
                row.updated_at = now
        self.session.flush()
        return replace(new)
