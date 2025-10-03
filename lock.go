package cache

import "sync"

// ---------------------------
// In-memory keyed mutex
// ---------------------------

type keyedMutex struct {
	mu    sync.Mutex
	locks map[string]chan struct{}
}

func newKeyedMutex() *keyedMutex {
	return &keyedMutex{locks: make(map[string]chan struct{})}
}

func (km *keyedMutex) ch(key string) chan struct{} {
	km.mu.Lock()
	defer km.mu.Unlock()
	ch, ok := km.locks[key]
	if !ok {
		ch = make(chan struct{}, 1)
		km.locks[key] = ch
	}
	return ch
}

func (km *keyedMutex) Lock(key string) func() {
	ch := km.ch(key)
	ch <- struct{}{}
	return func() { <-ch }
}

func (km *keyedMutex) TryLock(key string) (func(), bool) {
	ch := km.ch(key)
	select {
	case ch <- struct{}{}:
		return func() { <-ch }, true
	default:
		return func() {}, false
	}
}
