# YÖK Akademik Canlı Tarama

Render üzerinde çalıştırılmak üzere hazırlanmıştır.

- Build: `pip install -r requirements.txt`
- Start: `gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 180 app:app`
- Health check: `/`

Not: Render Free web service 15 dakika hareketsizlikten sonra uyuyabilir; ilk açılışta yeniden uyanması kısa bir süre alabilir.
