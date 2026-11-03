"""Which HTTPS certificates Datasquint trusts: for update checks (GitHub) and for the AI server.

Python normally trusts its own list of certificate authorities, not the one Windows and your browser use.
On company networks that inspect HTTPS with their own certificate, that gives
"CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate", although the browser works fine.

So, in order:
  1. CA_BUNDLE in .env: a file (or folder) with your company's root certificate(s), trusted in addition to
     Python's usual list. Use this when your IT department gives you a certificate file.
  2. Otherwise, with the `truststore` package installed (in requirements.txt, Python 3.10+): the operating
     system's own certificate store, exactly what Windows / macOS / the browser trust.
  3. Otherwise Python's default.

Certificate checks are never switched off: a failure is explained (see explain()) instead.
"""
import logging
import os
import ssl
import sys

log = logging.getLogger("datasquint.tls")

CA_BUNDLE = os.path.expanduser(os.getenv("CA_BUNDLE", "").strip())
_ctx, _source, _problem = None, None, None


def _build():
    global _ctx, _source, _problem
    if CA_BUNDLE:
        ctx = ssl.create_default_context()
        try:
            if os.path.isdir(CA_BUNDLE):
                ctx.load_verify_locations(capath=CA_BUNDLE)
            else:
                ctx.load_verify_locations(cafile=CA_BUNDLE)
            # Python 3.13+ also rejects certificates that miss formal RFC 5280 details (e.g. a company CA without a
            # key-usage field), which older inspection proxies often have. Chain, host name and dates are still
            # checked; only that strictness is relaxed, as in Python 3.12 and earlier.
            if hasattr(ssl, "VERIFY_X509_STRICT"):
                ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
            _ctx, _source = ctx, f"Python's list plus CA_BUNDLE ({CA_BUNDLE})"
            return
        except (OSError, ssl.SSLError) as e:
            _problem = f"CA_BUNDLE={CA_BUNDLE} couldn't be loaded ({e}); using the default certificates instead."
            log.warning(_problem)
    try:
        import truststore
        _ctx, _source = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT), "the system's certificate store (truststore)"
    except Exception:          # not installed, or Python older than 3.10
        _ctx, _source = ssl.create_default_context(), "Python's default certificates"


def context():
    """The SSL context to use for every HTTPS request Datasquint makes."""
    if _ctx is None:
        _build()
    return _ctx


def describe():
    context()
    return _source + (f" [{_problem}]" if _problem else "")


def is_cert_error(e):
    text = f"{e} {getattr(e, 'reason', '')} {e.__cause__ or ''} {e.__context__ or ''}"
    return "CERTIFICATE_VERIFY_FAILED" in text or "certificate verify failed" in text.lower()


def explain(what):
    """What to do about a certificate failure talking to `what` (e.g. 'GitHub', 'the AI server')."""
    context()
    if CA_BUNDLE and not _problem:
        hint = (f"CA_BUNDLE ({CA_BUNDLE}) doesn't contain the certificate that signs it: ask IT for the right "
                "root certificate (Base-64 .cer/.pem) and point CA_BUNDLE at it.")
    elif "truststore" in (_source or ""):
        system = {"win32": "Windows", "darwin": "macOS"}.get(sys.platform, "this computer")
        hint = (f"{system} doesn't trust it either, so it isn't a company certificate installed here: ask IT for "
                "the root certificate (Base-64 .cer/.pem) and set CA_BUNDLE=path/to/it in .env.")
    else:
        hint = ("Usually a company network that inspects HTTPS. Run `pip install truststore` so Datasquint trusts the "
                "same certificates as Windows and your browser, or set CA_BUNDLE=path/to/company-root.cer in .env.")
    return f"The HTTPS certificate of {what} isn't trusted. {hint} Then restart Datasquint."
