# -*- coding: utf-8 -*-
"""Configuração do gunicorn para o SGE em modo servidor.

IMPORTANTE: 1 worker. O SQLite aceita um escritor de cada vez; o paralelismo vem das threads.
"""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
workers = 1
worker_class = "gthread"
threads = 4
timeout = 120            # relatórios/PDF grandes
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
