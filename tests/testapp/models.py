from django.db import models


class Account(models.Model):
    name = models.CharField(max_length=100)
    status_id = models.IntegerField(default=1)


class Db(models.Model):
    host = models.CharField(max_length=200)
    user = models.CharField(max_length=100)
    password = models.CharField(max_length=200)
    account = models.OneToOneField(Account, on_delete=models.CASCADE, related_name='db', null=True)


class User(models.Model):
    email = models.EmailField()
    password = models.CharField(max_length=200, default='')
    account = models.ForeignKey(Account, on_delete=models.CASCADE, null=True)


class UserApi(models.Model):
    api_key = models.CharField(max_length=200)
    email = models.EmailField()
    name = models.CharField(max_length=100, default='')
    avatar = models.CharField(max_length=200, null=True)
    is_admin = models.BooleanField(default=False)
    is_owner = models.BooleanField(default=False)
    locale = models.CharField(max_length=10, default='en')
    preferences = models.JSONField(default=dict)
    timezone = models.CharField(max_length=50, default='UTC')


class Space(models.Model):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    secret = models.CharField(max_length=200, default='')
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
