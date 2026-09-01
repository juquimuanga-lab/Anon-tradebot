"""Solana arbitrage subsystem.

This package is intentionally isolated from the existing sniper scanners and
execution adapters. Arbitrage starts in observe/paper mode and only becomes a
trading path after an explicit execution implementation is added.
"""
