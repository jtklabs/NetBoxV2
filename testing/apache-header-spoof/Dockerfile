# Apache with the same modules as the production proxy (Ubuntu's apache2 and
# mod_auth_mellon), plus Python for the fake backend and the client.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends apache2 libapache2-mod-auth-mellon python3 \
 && rm -rf /var/lib/apt/lists/*
RUN a2enmod auth_mellon proxy proxy_http headers \
 && echo "ServerName localhost" > /etc/apache2/conf-available/servername.conf \
 && a2enconf servername
COPY vhost.conf /etc/apache2/sites-available/000-default.conf
COPY check.py /check.py
CMD ["python3", "-u", "/check.py"]
