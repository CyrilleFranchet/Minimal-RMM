/* Dedicated SCM service host. Stage1 executes as linked code on a worker thread. */
#define STAGE1_EMBEDDED 1
#include "../../stage1/c/stage1.c"
#pragma comment(lib, "advapi32.lib")

/* This host runs only under the SCM: a manual launch fails the dispatcher
 * and exits without staging. STOPPED is reported only after the linked
 * worker thread has completed, so a caller never sees success early. */
static SERVICE_STATUS_HANDLE g_serviceHandle;
static SERVICE_STATUS g_serviceStatus;
static SRWLOCK g_serviceLock = SRWLOCK_INIT;
static int g_serviceExit;

static void reportServiceState(DWORD state, DWORD result) {
    AcquireSRWLockExclusive(&g_serviceLock);
    ZeroMemory(&g_serviceStatus, sizeof(g_serviceStatus));
    g_serviceStatus.dwServiceType = SERVICE_WIN32_OWN_PROCESS;
    g_serviceStatus.dwCurrentState = state;
    if (state == SERVICE_START_PENDING) {
        g_serviceStatus.dwCheckPoint = 1;
        g_serviceStatus.dwWaitHint = 5000;
    }
    if (result) {
        g_serviceStatus.dwWin32ExitCode = ERROR_SERVICE_SPECIFIC_ERROR;
        g_serviceStatus.dwServiceSpecificExitCode = result;
    }
    SetServiceStatus(g_serviceHandle, &g_serviceStatus);
    ReleaseSRWLockExclusive(&g_serviceLock);
}

static void WINAPI stageServiceControl(DWORD control) {
    if (control == SERVICE_CONTROL_INTERROGATE) {
        AcquireSRWLockShared(&g_serviceLock);
        SetServiceStatus(g_serviceHandle, &g_serviceStatus);
        ReleaseSRWLockShared(&g_serviceLock);
    }
}

/* stage1 is linked into this service host and executes in its own native
 * worker thread. No separate stage1 executable or PE mapping is needed. */
static DWORD WINAPI stageWorker(LPVOID context) {
    (void)context;
    return (DWORD)runLauncher();
}

static void WINAPI stageServiceMain(DWORD argc, LPWSTR *argv) {
    const wchar_t *name = argc && argv && argv[0] ? argv[0] : L"MinimalRmmStage1";
    g_serviceHandle = RegisterServiceCtrlHandlerW(name, stageServiceControl);
    if (!g_serviceHandle) { g_serviceExit = 2; return; }
    reportServiceState(SERVICE_START_PENDING, 0);
    HANDLE worker = CreateThread(NULL, 0, stageWorker, NULL, 0, NULL);
    if (!worker) {
        g_serviceExit = 2;
        reportServiceState(SERVICE_STOPPED, (DWORD)g_serviceExit);
        return;
    }
    reportServiceState(SERVICE_RUNNING, 0);
    DWORD workerExit = 2;
    if (WaitForSingleObject(worker, INFINITE) != WAIT_OBJECT_0 ||
        !GetExitCodeThread(worker, &workerExit)) workerExit = 2;
    CloseHandle(worker);
    g_serviceExit = (int)workerExit;
    reportServiceState(SERVICE_STOPPED, (DWORD)g_serviceExit);
}

int WINAPI WinMain(HINSTANCE hInst, HINSTANCE hPrev,
                   LPSTR lpCmdLine, int nShowCmd) {
    (void)hInst; (void)hPrev; (void)lpCmdLine; (void)nShowCmd;
    SERVICE_TABLE_ENTRYW services[] = {
        { L"MinimalRmmStage1", stageServiceMain }, { NULL, NULL }
    };
    if (StartServiceCtrlDispatcherW(services)) return g_serviceExit;
    return 2;
}
