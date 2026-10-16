"""Shared pytest fixtures for the offline smoke tests."""

import pytest


@pytest.fixture
def tmp(tmp_path):
    """Provide the short temporary-directory fixture used by smoke tests."""
    return tmp_path
