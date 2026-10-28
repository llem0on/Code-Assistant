package user

import (
	"errors"
	"strings"
	"sync"
)

var (
	ErrNotFound   = errors.New("user not found")
	ErrEmailTaken = errors.New("email already used")
)

type User struct {
	ID    int64  `json:"id"`
	Email string `json:"email"`
	Name  string `json:"name"`
}

// Store keeps users in memory for local dev; Postgres comes later.
type Store struct {
	mu      sync.Mutex
	nextID  int64
	users   map[int64]User
	byEmail map[string]int64
}

func NewStore() *Store {
	return &Store{nextID: 1, users: map[int64]User{}, byEmail: map[string]int64{}}
}

// emailKey makes email uniqueness ignore letter case.
func emailKey(email string) string { return strings.ToLower(email) }

func (s *Store) Seed(u User) { _, _ = s.Create(u) }

func (s *Store) Create(u User) (User, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.byEmail[emailKey(u.Email)]; ok {
		return User{}, ErrEmailTaken
	}
	u.ID = s.nextID
	s.nextID++
	s.users[u.ID] = u
	s.byEmail[emailKey(u.Email)] = u.ID
	return u, nil
}

func (s *Store) Get(id int64) (User, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	u, ok := s.users[id]
	if !ok {
		return User{}, ErrNotFound
	}
	return u, nil
}

// Update changes only the fields that are not nil (partial update).
func (s *Store) Update(id int64, email, name *string) (User, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	u, ok := s.users[id]
	if !ok {
		return User{}, ErrNotFound
	}
	if email != nil {
		if other, ok := s.byEmail[emailKey(*email)]; ok && other != id {
			return User{}, ErrEmailTaken
		}
		delete(s.byEmail, emailKey(u.Email))
		u.Email = *email
		s.byEmail[emailKey(u.Email)] = id
	}
	if name != nil {
		u.Name = *name
	}
	s.users[id] = u
	return u, nil
}

// Delete removes the user for good (hard delete, per legal).
func (s *Store) Delete(id int64) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	u, ok := s.users[id]
	if !ok {
		return ErrNotFound
	}
	delete(s.byEmail, emailKey(u.Email))
	delete(s.users, id)
	return nil
}
