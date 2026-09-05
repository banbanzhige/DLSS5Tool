#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Persistent, validated state for the desktop batch export queue."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
import json
import os
import sys
import time
import uuid


QUEUE_STATES = {
    "pending", "running", "completed", "failed", "cancelled", "interrupted",
}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


@dataclass
class ExportJob:
    source_path: str
    output_path: str
    settings: dict
    export_settings: dict
    metadata: dict = field(default_factory=dict)
    color_info: dict = field(default_factory=dict)
    media_kind: str = "video"
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: str = "pending"
    progress_done: int = 0
    progress_total: int = 0
    progress_label: str = ""
    error: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0

    @classmethod
    def create(
        cls, source_path, output_path, settings, export_settings,
        metadata=None, color_info=None, media_kind=None,
    ):
        source_path = os.path.abspath(os.path.normpath(source_path))
        inferred_kind = (
            "image" if os.path.splitext(source_path)[1].lower() in _IMAGE_EXTENSIONS
            else "video"
        )
        return cls(
            source_path=source_path,
            output_path=os.path.abspath(os.path.normpath(output_path)),
            settings=deepcopy(settings or {}),
            export_settings=deepcopy(export_settings or {}),
            metadata=deepcopy(metadata or {}),
            color_info=deepcopy(color_info or {}),
            media_kind=media_kind if media_kind in {"video", "image"} else inferred_kind,
        )

    @classmethod
    def from_dict(cls, raw):
        if not isinstance(raw, dict):
            return None
        source = raw.get("source_path")
        output = raw.get("output_path")
        if not isinstance(source, str) or not source.strip():
            return None
        if not isinstance(output, str) or not output.strip():
            return None
        state = raw.get("state", "pending")
        if state not in QUEUE_STATES:
            state = "pending"
        if state == "running":
            state = "interrupted"
        inferred_kind = (
            "image" if os.path.splitext(source)[1].lower() in _IMAGE_EXTENSIONS
            else "video"
        )
        media_kind = raw.get("media_kind", inferred_kind)
        if media_kind not in {"video", "image"}:
            media_kind = inferred_kind
        def mapping(name):
            value = raw.get(name)
            return deepcopy(value) if isinstance(value, dict) else {}
        try:
            job = cls(
                source_path=os.path.abspath(os.path.normpath(source)),
                output_path=os.path.abspath(os.path.normpath(output)),
                settings=mapping("settings"),
                export_settings=mapping("export_settings"),
                metadata=mapping("metadata"),
                color_info=mapping("color_info"),
                media_kind=media_kind,
                job_id=str(raw.get("job_id") or uuid.uuid4().hex),
                state=state,
                progress_done=max(int(raw.get("progress_done", 0)), 0),
                progress_total=max(int(raw.get("progress_total", 0)), 0),
                progress_label=str(raw.get("progress_label") or ""),
                error=str(raw.get("error") or ""),
                created_at=float(raw.get("created_at", time.time())),
                started_at=float(raw.get("started_at", 0.0)),
                finished_at=float(raw.get("finished_at", 0.0)),
            )
        except (TypeError, ValueError, OverflowError):
            return None
        if state == "interrupted" and not job.error:
            job.error = "上次运行在任务完成前结束，可重试此任务。"
        return job

    def to_dict(self):
        return asdict(self)

    def reset_for_retry(self):
        self.state = "pending"
        self.progress_done = 0
        try:
            self.progress_total = max(int(self.metadata.get("frames", 0) or 0), 0)
        except (TypeError, ValueError, OverflowError):
            self.progress_total = 0
        self.progress_label = ""
        self.error = ""
        self.started_at = 0.0
        self.finished_at = 0.0


def queue_path():
    overridden = os.environ.get("DLSS5TOOL_QUEUE_PATH")
    if overridden:
        return os.path.abspath(overridden)
    base = (
        os.path.dirname(os.path.abspath(sys.executable))
        if getattr(sys, "frozen", False)
        else os.path.dirname(os.path.abspath(__file__))
    )
    return os.path.join(base, "dlss5_queue.json")


def load(path=None):
    path = os.path.abspath(path or queue_path())
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError, TypeError):
        return []
    rows = raw.get("jobs", []) if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return []
    jobs = []
    for item in rows:
        job = ExportJob.from_dict(item)
        if job is not None:
            jobs.append(job)
    return jobs


def save(jobs, path=None):
    path = os.path.abspath(path or queue_path())
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = path + ".tmp"
    payload = {
        "version": 1,
        "jobs": [job.to_dict() for job in jobs],
    }
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(temp, path)
