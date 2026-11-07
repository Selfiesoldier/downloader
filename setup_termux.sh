#!/data/data/com.termux/files/usr/bin/bash
set -e

echo "================================================="
echo "  Setting up Universal 2GB Downloader on Termux"
echo "================================================="

pkg update -y
pkg install -y python ffmpeg git

pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "⚠️ Created .env file. Please edit .env with your credentials:"
  echo "nano .env"
fi

echo "================================================="
echo "✅ Setup complete! Run with: python bot.py"
echo "================================================="
