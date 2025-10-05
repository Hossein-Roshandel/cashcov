package cache

import "sync"

// ---------------------------
// In-memory keyed mutex
// ---------------------------

type KeyedMutex struct {
	mu    sync.Mutex
	locks map[string]chan struct{}
}

func NewKeyedMutex() *KeyedMutex {
	return &KeyedMutex{locks: make(map[string]chan struct{})}
}

func (km *KeyedMutex) ch(key string) chan struct{} {
	km.mu.Lock()
	defer km.mu.Unlock()
	ch, ok := km.locks[key]
	if !ok {
		ch = make(chan struct{}, 1)
		km.locks[key] = ch
	}
	return ch
}

func (km *KeyedMutex) Lock(key string) func() {
	ch := km.ch(key)
	ch <- struct{}{}
	return func() { <-ch }
}

func (km *KeyedMutex) TryLock(key string) (func(), bool) {
	ch := km.ch(key)
	select {
	case ch <- struct{}{}:
		return func() { <-ch }, true
	default:
		return func() {}, false
	}
}
