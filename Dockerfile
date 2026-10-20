FROM python:3.12-slim
# 從靜態 ffmpeg 映像「只複製兩個執行檔」過來，取代 apt-get install ffmpeg
COPY --from=mwader/static-ffmpeg:7.1 /ffmpeg /ffprobe /usr/local/bin/
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY static ./static
ENV PYTHONUNBUFFERED=1 DATA_DIR=/srv/data
RUN mkdir -p /srv/data
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]