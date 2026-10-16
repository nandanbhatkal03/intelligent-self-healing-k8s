"""Background traffic for ShopLite.

The user count follows a slow sine wave (plus jitter) rather than a flat
line. Normal-operation training data then contains realistic load swings,
so the anomaly detector learns that "busier" is not the same as "broken"
(SRS §2.7: sufficient normal-operation telemetry before evaluation).
"""
import math
import random

from locust import HttpUser, LoadTestShape, between, task

MIN_USERS = 8
MAX_USERS = 30
PERIOD_S = 20 * 60          # one full low->high->low cycle every 20 minutes
PRODUCTS = ["p-100", "p-101", "p-102", "p-103", "p-104", "p-105"]


class Shopper(HttpUser):
    wait_time = between(0.5, 2.0)

    @task(5)
    def browse(self):
        self.client.get("/api/products", name="/api/products")

    @task(2)
    def checkout(self):
        self.client.post("/api/checkout", name="/api/checkout",
                         json={"product_id": random.choice(PRODUCTS),
                               "qty": random.randint(1, 3)})

    @task(1)
    def home(self):
        self.client.get("/", name="/")


class DiurnalShape(LoadTestShape):
    """Never-ending sinusoidal load. Returns (users, spawn_rate)."""

    def tick(self):
        t = self.get_run_time()
        mid = (MAX_USERS + MIN_USERS) / 2
        amp = (MAX_USERS - MIN_USERS) / 2
        users = mid + amp * math.sin(2 * math.pi * t / PERIOD_S)
        users += random.uniform(-2, 2)
        return max(1, round(users)), 5
