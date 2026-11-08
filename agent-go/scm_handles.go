package main

import "errors"

// scmServiceHandles owns a service and manager plus their native operations.
// Keeping ownership independent of Win32 permits deterministic failure tests.
type scmServiceHandles struct {
	manager, service uintptr
	closeHandle      func(uintptr) error
	deleteService    func(uintptr) error
}

// close releases the service before the manager. Failed handles stay owned so
// deferred cleanup can retry; successful closes are never repeated.
func (h *scmServiceHandles) close() error {
	var serviceErr, managerErr error
	if h.service != 0 {
		serviceErr = h.closeHandle(h.service)
		if serviceErr == nil {
			h.service = 0
		}
	}
	if h.manager != 0 {
		managerErr = h.closeHandle(h.manager)
		if managerErr == nil {
			h.manager = 0
		}
	}
	return errors.Join(serviceErr, managerErr)
}

// delete marks a stopped service for deletion and releases owned handles before
// binary removal. Both deletion and close errors remain visible to the caller.
func (h *scmServiceHandles) delete() (bool, error) {
	deleteErr := h.deleteService(h.service)
	return deleteErr == nil, errors.Join(deleteErr, h.close())
}
