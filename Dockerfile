FROM python:3.7-slim-buster
RUN echo 'Acquire::http::Proxy "http://proxies.labs:3142/apt-cacher/";' > /etc/apt/apt.conf.d/01proxy
RUN apt-get update && apt-get install --no-install-recommends -y make python3 python3-pip python3-distutils

RUN mkdir -p /usr/src 
WORKDIR /usr/src/app/

RUN python3 -m venv venv
RUN . venv/bin/activate
ADD pyproject.toml .
ADD poetry.lock .
RUN venv/bin/pip install poetry
RUN poetry install

COPY . /usr/src/app/
RUN PATH="$PATH:venv/bin" make all
