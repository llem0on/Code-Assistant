package main

import (
	"log"
	"net/http"
	"os"

	"usersvc/internal/user"
)

func main() {
	port := os.Getenv("PORT")
	if port == "" {
		port = "18080"
	}

	store := user.NewStore()
	store.Seed(user.User{Email: "exists@test.co", Name: "Seed"})

	mux := http.NewServeMux()
	user.NewHandler(store).Register(mux)

	log.Printf("users service listening on 127.0.0.1:%s", port)
	log.Fatal(http.ListenAndServe("127.0.0.1:"+port, mux))
}
