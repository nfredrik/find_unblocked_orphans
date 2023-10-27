FROM fedora:latest

# Uppdatera paketlistan och installera Python
RUN dnf -y update && dnf -y install python3 python3-dnf python-dns python3-pip krb5-devel gcc  python3-devel

#RUN pip3 install dnf dogpile requests koji
COPY scratch.py /
# Exempel: Ange Python som standardkommando när containern körs
CMD ["python3"]