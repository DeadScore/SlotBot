# Volles Python 3.11 Image nutzen (inkl. audioop)
FROM python:3.11

# Arbeitsverzeichnis im Container setzen
WORKDIR /app

# Projektdateien kopieren
COPY . /app

# Pip aktualisieren und Abhängigkeiten installieren
RUN pip install --upgrade pip
RUN pip install -r requirements.txt

# Startkommando
CMD ["python", "main.py"]
