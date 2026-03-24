from fastapi import Request
from motor.motor_asyncio import AsyncIOMotorDatabase
import redis.asyncio as aioredis

from app.clients import MongoClient, RedisClient, S3Client, FirebaseClient
from app.config import Settings


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_mongo(request: Request) -> MongoClient:
    return request.app.state.mongo


def get_db(request: Request) -> AsyncIOMotorDatabase:
    return request.app.state.mongo.db


def get_redis(request: Request) -> RedisClient:
    return request.app.state.redis


def get_redis_client(request: Request) -> aioredis.Redis:
    return request.app.state.redis.client


def get_s3(request: Request) -> S3Client:
    return request.app.state.s3


def get_firebase(request: Request) -> FirebaseClient:
    return request.app.state.firebase
