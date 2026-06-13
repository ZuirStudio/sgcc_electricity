FROM tablebird/sgcc_electricity_base:latest

ENV PYTHON_IN_DOCKER='PYTHON_IN_DOCKER'

COPY ./requirements.txt /tmp/requirements.txt

RUN cd /tmp \
    && python3 -m pip install --upgrade pip \
    && PIP_ROOT_USER_ACTION=ignore pip install \
    --disable-pip-version-check \
    --no-cache-dir \
    -r requirements.txt \
    && rm -rf /tmp/* \
    && pip cache purge \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /var/log/*

COPY scripts/* /app/
WORKDIR /app

ARG VERSION
ENV VERSION=${VERSION}

CMD ["python3","main.py"]