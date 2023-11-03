FROM fedora:latest

# Uppdatera paketlistan och installera Python
RUN dnf -y update && dnf -y install python3 python3-dnf python-dns python3-pip krb5-devel gcc  python3-devel

RUN pip3 install dogpile-cache requests koji
COPY releases.py pagure_info.py main.py information.py deep_checker.py orphans.py /

RUN chmod +x main.py
# Exempel: Ange Python som standardkommando när containern körs
#CMD ["python3"]
CMD ["./main.py"]