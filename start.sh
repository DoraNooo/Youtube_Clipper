#!/bin/bash
cd "$(dirname "$0")"
source venv/bin/activate
echo "YouTube Clipper démarré → http://localhost:5000"
python app.py
