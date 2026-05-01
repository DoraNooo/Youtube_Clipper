Maintenance VPS — YouTube Clipper
Renouveler les cookies YouTube
À faire quand l'app affiche "Sign in to confirm you're not a bot".

Sur ton Mac, dans Chrome/Firefox connecté à YouTube, exporte les cookies au format Netscape (extension Get cookies.txt LOCALLY ou équivalent).

Copier le fichier vers le VPS :

scp ~/Downloads/cookies.txt clipper@178.105.48.96:/home/clipper/cookies.txt


Redémarrer le service :
sudo systemctl restart clip-youtube


Mettre à jour yt-dlp et les plugins
/home/clipper/Clip_youtube/venv/bin/pip install -U \
  yt-dlp \
  yt-dlp-ejs \
  bgutil-ytdlp-pot-provider



Redémarrer le service :

sudo systemctl restart clip-youtube


Mettre à jour le conteneur bgutil (PO Token)
sudo docker pull brainicism/bgutil-ytdlp-pot-provider
sudo docker stop bgutil-provider
sudo docker rm bgutil-provider
sudo docker run -d \
  --restart unless-stopped \
  --name bgutil-provider \
  -p 127.0.0.1:4416:4416 \
  brainicism/bgutil-ytdlp-pot-provider


Vérifier que tout tourne
# Service app
sudo systemctl status clip-youtube --no-pager
# Conteneur bgutil
sudo docker ps | grep bgutil
curl -s http://127.0.0.1:4416/ping
# Logs en direct
sudo journalctl -u clip-youtube -f