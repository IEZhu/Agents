"""Owner-only access on Windows, where chmod 0700/0600 only toggles the read-only flag.

``restrict`` gives a path a protected DACL with one ACE, full control for the current user,
inherited by everything created below it later. ``is_private`` holds when every ACE that
allows anything names the current user: a token or a bridge configuration inside a
restricted directory passes, one that inherits the profile's ACL (SYSTEM, Administrators)
does not. ``owned_by_user_or_admins`` is the counterpart of the POSIX owner check: the owner
may rewrite the DACL at any time, and an elevated administrator's new files are owned by the
Administrators group rather than the user. Standard library only.
"""
import ctypes
from ctypes import wintypes
import os

if os.name == "nt":
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p
    _advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    _advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                              ctypes.POINTER(wintypes.DWORD)]
    _advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    _advapi32.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
                                                    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
    _advapi32.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p,
                                                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    _advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    _advapi32.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
                                                ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                                ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                                ctypes.POINTER(ctypes.c_void_p)]
    _advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    _advapi32.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    _advapi32.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]

_OWNER_SECURITY_INFORMATION = 0x00000001
_ADMINISTRATORS = "S-1-5-32-544"
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_SE_FILE_OBJECT = 1
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SDDL_REVISION_1 = 1
_ACCESS_ALLOWED_ACE_TYPE, _ACCESS_DENIED_ACE_TYPE = 0, 1
_OBJECT_INHERIT_ACE, _CONTAINER_INHERIT_ACE, _NO_PROPAGATE_INHERIT_ACE = 0x1, 0x2, 0x4
_SID_OFFSET = 8  # ACE_HEADER (4 bytes) and ACCESS_MASK (4 bytes) precede SidStart


class _AclHeader(ctypes.Structure):
    _fields_ = [("AclRevision", ctypes.c_ubyte), ("Sbz1", ctypes.c_ubyte), ("AclSize", wintypes.WORD),
                ("AceCount", wintypes.WORD), ("Sbz2", wintypes.WORD)]


class _AceHeader(ctypes.Structure):
    _fields_ = [("AceType", ctypes.c_ubyte), ("AceFlags", ctypes.c_ubyte), ("AceSize", wintypes.WORD)]


def _check(succeeded):
    if not succeeded:
        raise ctypes.WinError(ctypes.get_last_error())


class _UserSid:
    """The current process user's SID, as a buffer that ``EqualSid`` can read and as text."""

    def __init__(self):
        token = wintypes.HANDLE()
        _check(_advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)))
        try:
            size = wintypes.DWORD()
            _advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
            self._buffer = ctypes.create_string_buffer(size.value)  # TOKEN_USER; its SID lives in this buffer
            _check(_advapi32.GetTokenInformation(token, _TOKEN_USER, self._buffer, size, ctypes.byref(size)))
        finally:
            _kernel32.CloseHandle(token)
        self.pointer = ctypes.c_void_p.from_buffer(self._buffer).value  # TOKEN_USER.User.Sid
        text = wintypes.LPWSTR()
        _check(_advapi32.ConvertSidToStringSidW(self.pointer, ctypes.byref(text)))
        try:
            self.text = text.value
        finally:
            _kernel32.LocalFree(text)


def user_sid() -> str:
    """The current user's SID, for example ``S-1-5-21-…-500``."""
    return _UserSid().text


def restrict(path) -> None:
    """Replace the DACL of ``path`` with full control for the current user only, protected from
    inheritance and inherited by files and directories created below it."""
    descriptor = ctypes.c_void_p()
    _check(_advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        f"D:P(A;OICI;FA;;;{user_sid()})", _SDDL_REVISION_1, ctypes.byref(descriptor), None))
    try:
        present, defaulted, dacl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
        _check(_advapi32.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(dacl),
                                                   ctypes.byref(defaulted)))
        error = _advapi32.SetNamedSecurityInfoW(
            str(path), _SE_FILE_OBJECT, _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, dacl, None)
        if error:
            raise ctypes.WinError(error)
    finally:
        _kernel32.LocalFree(descriptor)


def owned_by_user_or_admins(path) -> bool:
    """Whether ``path`` is owned by the current user or by the Administrators group."""
    descriptor, owner = ctypes.c_void_p(), ctypes.c_void_p()
    error = _advapi32.GetNamedSecurityInfoW(str(path), _SE_FILE_OBJECT, _OWNER_SECURITY_INFORMATION,
                                            ctypes.byref(owner), None, None, None, ctypes.byref(descriptor))
    if error:
        raise ctypes.WinError(error)
    admins = ctypes.c_void_p()
    try:
        if not owner.value:
            return False
        user = _UserSid()  # held while EqualSid reads the SID inside its buffer
        if _advapi32.EqualSid(owner, user.pointer):
            return True
        _check(_advapi32.ConvertStringSidToSidW(_ADMINISTRATORS, ctypes.byref(admins)))
        return bool(_advapi32.EqualSid(owner, admins))
    finally:
        if admins.value:
            _kernel32.LocalFree(admins)
        _kernel32.LocalFree(descriptor)


def is_private(path, *, inherited_below=False) -> bool:
    """Whether only the current user is allowed any access to ``path``.

    A missing or NULL DACL (everyone allowed), an ACE type other than plain allow and deny,
    or an allow ACE for any other SID is not private; a deny ACE only takes access away.
    With ``inherited_below`` a directory also needs an allow ACE that files and subdirectories
    created anywhere below it inherit: without one, Windows gives them the creator's default
    DACL. An ACE that stops at the first level (``NO_PROPAGATE_INHERIT_ACE``) does not count.
    """
    descriptor, dacl = ctypes.c_void_p(), ctypes.c_void_p()
    error = _advapi32.GetNamedSecurityInfoW(str(path), _SE_FILE_OBJECT, _DACL_SECURITY_INFORMATION,
                                            None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if error:
        raise ctypes.WinError(error)
    try:
        if not dacl.value:
            return False
        user = _UserSid()
        header = _AclHeader.from_address(dacl.value)
        allowed, passed_on = 0, False
        both = _OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE
        flags = both | _NO_PROPAGATE_INHERIT_ACE
        for index in range(header.AceCount):
            ace = ctypes.c_void_p()
            _check(_advapi32.GetAce(dacl, index, ctypes.byref(ace)))
            ace_header = _AceHeader.from_address(ace.value)
            if ace_header.AceType == _ACCESS_DENIED_ACE_TYPE:
                continue
            if (ace_header.AceType != _ACCESS_ALLOWED_ACE_TYPE
                    or not _advapi32.EqualSid(ace.value + _SID_OFFSET, user.pointer)):
                return False
            allowed += 1
            passed_on = passed_on or ace_header.AceFlags & flags == both
        return allowed > 0 and (passed_on or not inherited_below)
    finally:
        _kernel32.LocalFree(descriptor)
