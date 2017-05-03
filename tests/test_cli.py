import pwd
from click.testing import CliRunner
from datetime import datetime, timedelta
from time import sleep
import pytest
import shutil
import os

runner = CliRunner()

@pytest.fixture(scope='module')
def client():
    from certidude.api import certidude_app
    from falcon import testing
    app = certidude_app()
    return testing.TestClient(app)

def generate_csr(cn=None):
    from cryptography import x509
    from cryptography.hazmat.primitives.asymmetric import rsa, padding
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.backends import default_backend
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=1024,
        backend=default_backend())
    csr = x509.CertificateSigningRequestBuilder()
    if cn is not None:
        csr = csr.subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
    buf = csr.sign(key, hashes.SHA256(), default_backend()
        ).public_bytes(serialization.Encoding.PEM)
    return buf

def test_cli_setup_authority():
    import os
    import sys

    assert os.getuid() == 0, "Run tests as root in a clean VM or container"

    if os.path.exists("/run/certidude/signer.pid"):
        with open("/run/certidude/signer.pid") as fh:
            try:
                os.kill(int(fh.read()), 15)
            except OSError:
                pass
    if os.path.exists("/run/certidude/server.pid"):
        with open("/run/certidude/server.pid") as fh:
            try:
                os.kill(int(fh.read()), 15)
            except OSError:
                pass

    if os.path.exists("/var/lib/certidude/ca.example.lan"):
        shutil.rmtree("/var/lib/certidude/ca.example.lan")
    if os.path.exists("/etc/certidude/server.conf"):
        os.unlink("/etc/certidude/server.conf")
    if os.path.exists("/etc/certidude/client.conf"):
        os.unlink("/etc/certidude/client.conf")
    if os.path.exists("/run/certidude"):
        shutil.rmtree("/run/certidude")

    # Remove OpenVPN stuff
    if os.path.exists("/etc/openvpn"):
        for filename in os.listdir("/etc/openvpn"):
            if filename.endswith(".conf"):
                os.unlink(os.path.join("/etc/openvpn", filename))
        if os.path.exists("/etc/openvpn/keys"):
            shutil.rmtree("/etc/openvpn/keys")

    from certidude.cli import entry_point as cli
    from certidude import const

    result = runner.invoke(cli, ['setup', 'authority'])
    os.setgid(0) # Restore GID
    os.umask(0022)

    assert not result.exception, result.output
    assert os.getuid() == 0 and os.getgid() == 0, "Serve dropped permissions incorrectly!"


    from certidude import config, authority
    assert authority.ca_cert.serial_number >= 0x100000000000000000000000000000000000000
    assert authority.ca_cert.serial_number <= 0xfffffffffffffffffffffffffffffffffffffff
    assert authority.ca_cert.not_valid_before < datetime.now()
    assert authority.ca_cert.not_valid_after > datetime.now() + timedelta(days=7000)

    # Start server before any signing operations are performed
    config.CERTIFICATE_RENEWAL_ALLOWED = True

    server_pid = os.fork()
    if not server_pid:
        # Fork to prevent umask, setuid, setgid side effects
        result = runner.invoke(cli, ['serve', '-p', '80', '-l', '127.0.1.1'])
        assert not result.exception, result.output
        return

    sleep(1) # Wait for serve to start up

    import requests

    # Test CA certificate fetch
    buf = open("/var/lib/certidude/ca.example.lan/ca_crt.pem").read()
    r = requests.get("http://ca.example.lan/api/certificate")
    assert r.status_code == 200
    assert r.headers.get('content-type') == "application/x-x509-ca-cert"
    assert r.text == buf


    # Password is bot, users created by Travis
    usertoken = "Basic dXNlcmJvdDpib3Q="
    admintoken = "Basic YWRtaW5ib3Q6Ym90"

    result = runner.invoke(cli, ['users'])
    assert not result.exception, result.output

    # Check that we can retrieve empty CRL
    assert authority.export_crl(), "Failed to export CRL"
    r = requests.get("http://ca.example.lan/api/revoked/")
    assert r.status_code == 200, r.text


    # Test command line interface
    result = runner.invoke(cli, ['list', '-srv'])
    assert not result.exception, result.output

    # Test static
    r = requests.get("http://ca.example.lan/index.html")
    assert r.status_code == 200, r.text # if this breaks certidude serve has no read access to static folder
    r = requests.get("http://ca.example.lan/nonexistant.html")
    assert r.status_code == 404, r.text

    # Test request submission
    buf = generate_csr(cn=u"test")

    r = client().simulate_post("/api/request/", body=buf)
    assert r.status_code == 415 # wrong content type

    r = client().simulate_post("/api/request/",
        body=buf,
        headers={"content-type":"application/pkcs10"})
    assert r.status_code == 202 # success

    r = client().simulate_post("/api/request/",
        body=buf,
        headers={"content-type":"application/pkcs10"})
    assert r.status_code == 202 # already exists, same keypair so it's ok

    r = client().simulate_post("/api/request/",
        query_string="wait=true",
        body=buf,
        headers={"content-type":"application/pkcs10"})
    assert r.status_code == 303 # redirect to long poll

    r = client().simulate_post("/api/request/",
        body=generate_csr(cn=u"test"),
        headers={"content-type":"application/pkcs10"})
    assert r.status_code == 409 # duplicate cn, different keypair

    r = client().simulate_get("/api/request/test/", headers={"Accept":"application/json"})
    assert r.status_code == 200 # fetch as JSON ok
    assert r.headers.get('content-type') == "application/json"

    r = client().simulate_get("/api/request/test/", headers={"Accept":"application/x-pem-file"})
    assert r.status_code == 200 # fetch as PEM ok
    assert r.headers.get('content-type') == "application/x-pem-file"

    r = client().simulate_get("/api/request/test/", headers={"Accept":"text/plain"})
    assert r.status_code == 415 # not available as plaintext

    r = client().simulate_get("/api/request/nonexistant/", headers={"Accept":"application/json"})
    assert r.status_code == 404 # nonexistant common names

    r = client().simulate_post("/api/request/",
        query_string="autosign=1",
        body=buf,
        headers={"content-type":"application/pkcs10"})
    assert r.status_code == 200 # autosign successful
    assert r.headers.get('content-type') == "application/x-pem-file"

    # TODO: submit messed up CSR-s: no CN, empty CN etc

    # Test command line interface
    result = runner.invoke(cli, ['list', '-srv'])
    assert not result.exception, result.output

    # Some commands have side effects (setuid, setgid etc)
    child_pid = os.fork()
    if not child_pid:
        result = runner.invoke(cli, ['sign', 'test', '-o'])
        assert not result.exception, result.output
        return
    else:
        os.waitpid(child_pid, 0)
    assert os.getuid() == 0 and os.getgid() == 0, "Serve dropped permissions incorrectly!"

    # Test session API call
    r = client().simulate_get("/api/", headers={"Authorization":usertoken})
    assert r.status_code == 200

    r = client().simulate_get("/api/", headers={"Authorization":admintoken})
    assert r.status_code == 200

    r = client().simulate_get("/api/")
    assert r.status_code == 401


    # Test signed certificate API call
    r = client().simulate_get("/api/signed/nonexistant/")
    assert r.status_code == 404, r.text

    r = client().simulate_get("/api/signed/test/")
    assert r.status_code == 200, r.text
    assert r.headers.get('content-type') == "application/x-pem-file"

    r = client().simulate_get("/api/signed/test/", headers={"Accept":"application/json"})
    assert r.status_code == 200, r.text
    assert r.headers.get('content-type') == "application/json"

    r = client().simulate_get("/api/signed/test/", headers={"Accept":"text/plain"})
    assert r.status_code == 415, r.text

    # Test revocations API call
    r = client().simulate_get("/api/revoked/",
        headers={"Accept":"application/x-pem-file"})
    assert r.status_code == 200, r.text # if this breaks certidude serve has no access to signer socket
    assert r.headers.get('content-type') == "application/x-pem-file"

    r = client().simulate_get("/api/revoked/")
    assert r.status_code == 200, r.text
    assert r.headers.get('content-type') == "application/x-pkcs7-crl"

    r = client().simulate_get("/api/revoked/",
        headers={"Accept":"text/plain"})
    assert r.status_code == 415, r.text

    r = client().simulate_get("/api/revoked/",
        query_string="wait=true",
        headers={"Accept":"application/x-pem-file"})
    assert r.status_code == 303, r.text

    # Test attribute fetching API call
    r = client().simulate_get("/api/signed/test/attr/")
    assert r.status_code == 403, r.text
    r = client().simulate_get("/api/signed/test/lease/", headers={"Authorization":admintoken})
    assert r.status_code == 404, r.text

    # Insert lease as if VPN gateway had submitted it
    path, _, _ = authority.get_signed("test")
    from xattr import setxattr
    setxattr(path, "user.lease.address", b"127.0.0.1")
    setxattr(path, "user.lease.last_seen", b"random")
    r = client().simulate_get("/api/signed/test/attr/")
    assert r.status_code == 200, r.text

    # Test lease retrieval
    r = client().simulate_get("/api/signed/test/lease/")
    assert r.status_code == 401, r.text
    r = client().simulate_get("/api/signed/test/lease/", headers={"Authorization":usertoken})
    assert r.status_code == 403, r.text
    r = client().simulate_get("/api/signed/test/lease/", headers={"Authorization":admintoken})
    assert r.status_code == 200, r.text
    assert r.headers.get('content-type') == "application/json; charset=UTF-8"


    # Tags should not be visible anonymously
    r = client().simulate_get("/api/signed/test/tag/")
    assert r.status_code == 401, r.text
    r = client().simulate_get("/api/signed/test/tag/", headers={"Authorization":usertoken})
    assert r.status_code == 403, r.text
    r = client().simulate_get("/api/signed/test/tag/", headers={"Authorization":admintoken})
    assert r.status_code == 200, r.text

    # Tags can be added only by admin
    r = client().simulate_post("/api/signed/test/tag/")
    assert r.status_code == 401, r.text
    r = client().simulate_post("/api/signed/test/tag/",
        headers={"Authorization":usertoken})
    assert r.status_code == 403, r.text
    r = client().simulate_post("/api/signed/test/tag/",
        body="key=other&value=something",
        headers={"content-type": "application/x-www-form-urlencoded", "Authorization":admintoken})
    assert r.status_code == 200, r.text

    # Tags can be overwritten only by admin
    r = client().simulate_put("/api/signed/test/tag/other/")
    assert r.status_code == 401, r.text
    r = client().simulate_put("/api/signed/test/tag/other/",
        headers={"Authorization":usertoken})
    assert r.status_code == 403, r.text
    r = client().simulate_put("/api/signed/test/tag/other/",
        body="value=else",
        headers={"content-type": "application/x-www-form-urlencoded", "Authorization":admintoken})
    assert r.status_code == 200, r.text

    # Tags can be deleted only by admin
    r = client().simulate_delete("/api/signed/test/tag/else/")
    assert r.status_code == 401, r.text
    r = client().simulate_delete("/api/signed/test/tag/else/",
        headers={"Authorization":usertoken})
    assert r.status_code == 403, r.text
    r = client().simulate_delete("/api/signed/test/tag/else/",
        headers={"content-type": "application/x-www-form-urlencoded", "Authorization":admintoken})
    assert r.status_code == 200, r.text


    # Test revocation
    r = client().simulate_delete("/api/signed/test/")
    assert r.status_code == 401, r.text
    r = client().simulate_delete("/api/signed/test/",
        headers={"Authorization":usertoken})
    assert r.status_code == 403, r.text
    r = client().simulate_delete("/api/signed/test/",
        headers={"Authorization":admintoken})
    assert r.status_code == 200, r.text


    # Log can be read only by admin
    r = client().simulate_get("/api/log/")
    assert r.status_code == 401, r.text
    r = client().simulate_get("/api/log/",
        headers={"Authorization":usertoken})
    assert r.status_code == 403, r.text
    r = client().simulate_get("/api/log/",
        headers={"Authorization":admintoken})
    assert r.status_code == 200, r.text
    assert r.headers.get('content-type') == "application/json; charset=UTF-8"

    # Test token mech
    r = client().simulate_post("/api/token/")
    assert r.status_code == 404, r.text

    config.BUNDLE_FORMAT = "ovpn"
    config.USER_ENROLLMENT_ALLOWED = True

    r = client().simulate_post("/api/token/")
    assert r.status_code == 401 # needs auth
    r = client().simulate_post("/api/token/",
        headers={"Authorization":usertoken})
    assert r.status_code == 403 # regular user forbidden
    r = client().simulate_post("/api/token/",
        body="user=userbot", # TODO: test nonexistant user
        headers={"content-type": "application/x-www-form-urlencoded", "Authorization":admintoken})
    assert r.status_code == 200 # token generated by admin

    r2 = client().simulate_get("/api/token/",
        query_string="u=userbot&t=1493184342&c=ac9b71421d5741800c5a4905b20c1072594a2df863e60ba836464888786bf2a6",
        headers={"content-type": "application/x-www-form-urlencoded", "Authorization":admintoken})
    assert r2.status_code == 403 # invalid checksum
    r2 = client().simulate_get("/api/token/",
        query_string=r.content,
        headers={"User-Agent":"Mozilla/5.0 (X11; Fedora; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/57.0.2987.133 Safari/537.36"})
    assert r2.status_code == 200 # token consumed by anyone on Fedora
    assert r2.headers.get('content-type') == "application/x-openvpn"

    config.BUNDLE_FORMAT = "p12" # Switch to PKCS#12
    r2 = client().simulate_get("/api/token/", query_string=r.content)
    assert r2.status_code == 200 # token consumed by anyone on unknown device
    assert r2.headers.get('content-type') == "application/x-pkcs12"

    result = runner.invoke(cli, ['setup', 'openvpn', 'server', "-cn", "vpn.example.lan", "ca.example.lan"])
    assert not result.exception, result.output

    result = runner.invoke(cli, ['setup', 'openvpn', 'client', "-cn", "roadwarrior1", "ca.example.lan", "vpn.example.lan"])
    assert not result.exception, result.output

    import os
    if not os.path.exists("/etc/openvpn/keys"):
        os.makedirs("/etc/openvpn/keys")

    with open("/etc/certidude/client.conf", "a") as fh:
        fh.write("insecure = true\n")

    # pregen dhparam
    result = runner.invoke(cli, ["request", "--no-wait"])
    assert not result.exception, "server responded %s, server logs say %s"  % (result.output, open("/var/log/certidude.log").read())

    child_pid = os.fork()
    if not child_pid:
        result = runner.invoke(cli, ['sign', 'vpn.example.lan'])
        assert not result.exception, result.output
        return
    else:
        os.waitpid(child_pid, 0)

    result = runner.invoke(cli, ["request", "--no-wait"])
    assert not result.exception, result.output
    result = runner.invoke(cli, ["request", "--renew"])
    assert not result.exception, result.output

    # Test revocation on command-line
    child_pid = os.fork()
    if not child_pid:
        result = runner.invoke(cli, ['revoke', 'vpn.example.lan'])
        assert not result.exception, result.output
        return
    else:
        os.waitpid(child_pid, 0)

    result = runner.invoke(cli, ['list', '-srv'])
    assert not result.exception, result.output
    result = runner.invoke(cli, ['cron'])
    assert not result.exception, result.output

    # Shut down signer
    assert authority.signer_exec("exit") == "ok"

    # Shut down server
    with open("/run/certidude/server.pid") as fh:
        os.kill(int(fh.read()), 1)

    os.waitpid(server_pid, 0)
