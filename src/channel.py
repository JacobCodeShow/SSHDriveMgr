"""Helpers for app name/version display formatting."""

from __future__ import annotations


APP_NAME = "SSH 磁盘管理器"


def current_channel() -> str:
    return ""


def channel_badge() -> str:
    return ""


def display_name(base_name: str = APP_NAME) -> str:
    return base_name


def display_version(version: str) -> str:
    return f"v{version}"