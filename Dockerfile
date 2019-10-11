FROM kshenk/centos:7.7

LABEL ksgh.maintainer="k.shenk@gmail.com"

RUN yum install -y python2 python2-pip && \
    pip install --upgrade pip && \
    yum clean all

COPY ./src/requirements.txt /
COPY ./src/boto.cfg /etc/
COPY ./src/slavecheck.py /

RUN pip install -r /requirements.txt

ENTRYPOINT ["/usr/bin/env", "python"]

CMD ["/slavecheck.py"]
