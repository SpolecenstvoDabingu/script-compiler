#!/bin/sh
set -e

gunicorn main:app -w 4 -b 0.0.0.0:8000 & nginx -g 'daemon off;'
