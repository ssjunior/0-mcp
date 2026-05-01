from .base import BaseResource
from .calc import get_results


class Metrics(BaseResource):
    allowed_methods = ['post']

    async def post(self, request):
        timezone = self.user.get('timezone', 'UTC')
        results = await get_results(timezone, self.body)
        return results
