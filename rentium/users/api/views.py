from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.mixins import ListModelMixin, RetrieveModelMixin, UpdateModelMixin
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet
from rest_framework.permissions import IsAuthenticated

from rentium.users.models import User, LandlordProfile, TenantProfile
from .serializers import UserSerializer, LandlordProfileSerializer, TenantProfileSerializer


class UserViewSet(RetrieveModelMixin, ListModelMixin, UpdateModelMixin, GenericViewSet):
    serializer_class = UserSerializer
    queryset = User.objects.all()
    lookup_field = "pk"
    permission_classes = [IsAuthenticated]

    def get_queryset(self, *args, **kwargs):
        assert isinstance(self.request.user.id, int)
        return self.queryset.filter(id=self.request.user.id)

    @action(detail=False)
    def me(self, request):
        serializer = UserSerializer(request.user, context={"request": request})
        return Response(status=status.HTTP_200_OK, data=serializer.data)

class LandlordProfileViewSet(viewsets.ModelViewSet):
    serializer_class = LandlordProfileSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        return LandlordProfile.objects.filter(user=self.request.user)

class TenantProfileViewSet(viewsets.ModelViewSet):
    serializer_class = TenantProfileSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        return TenantProfile.objects.filter(user=self.request.user)
