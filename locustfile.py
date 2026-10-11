import random
from locust import FastHttpUser, task, constant


class APIUser(FastHttpUser):
    wait_time = constant(0)

    @task(10)
    def get_user(self):
        self.client.get(f"/users/{random.randint(1, 1000)}", name="/users/[id]")

    @task(1)
    def health(self):
        self.client.get("/health")