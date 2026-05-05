from django import template
from core.models import Factory

register = template.Library()


@register.filter(name='is_factory_user')
def is_factory_user(user):
    """Template filter: {{ user|is_factory_user }} → True/False"""
    if not user or not user.is_authenticated:
        return False
    return Factory.objects.filter(user=user).exists()
