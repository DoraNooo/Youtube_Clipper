#!/bin/bash
cd "$(dirname "$0")"
source venv/bin/activate
echo "YouTube Clipper démarré → http://localhost:5000"
export YT_CLIPPER_BACKUP_SECRET="Roc04233cloud"
python app.py
