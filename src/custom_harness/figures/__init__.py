"""Figures as audited outputs (GOAL §5 L2): see figures/audit.py."""

from .audit import FigureAudit, FigureError, audit_figure, audit_figures, check_svg, make_figure, resolve_refs

__all__ = ["FigureAudit", "FigureError", "audit_figure", "audit_figures", "check_svg", "make_figure", "resolve_refs"]
