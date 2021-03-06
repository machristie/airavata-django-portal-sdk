import logging
import mimetypes
import os
import shutil
from urllib.parse import urlparse

from django.apps import apps
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, SuspiciousFileOperation
from django.core.files.move import file_move_safe
from django.core.files.storage import FileSystemStorage

from airavata.model.data.replica.ttypes import (DataProductModel,
                                                DataProductType,
                                                DataReplicaLocationModel,
                                                ReplicaLocationCategory,
                                                ReplicaPersistentType)

import copy

logger = logging.getLogger(__name__)

TMP_INPUT_FILE_UPLOAD_DIR = "tmp"


def save(request, path, file, name=None, content_type=None):
    "Save file in path in the user's storage."
    username = request.user.username
    full_path = _Datastore().save(username, path, file, name=name)
    data_product = _save_data_product(
        request, full_path, name=name, content_type=content_type
    )
    return data_product


def move_from_filepath(request, source_path, target_path, name=None, content_type=None):
    "Move a file from filesystem into user's storage."
    username = request.user.username
    file_name = name if name is not None else os.path.basename(source_path)
    full_path = _Datastore().move_external(source_path, username, target_path, file_name)
    data_product = _save_data_product(
        request, full_path, name=file_name, content_type=content_type
    )
    return data_product


def save_input_file(request, file, name=None, content_type=None):
    """Save input file in staging area for input files."""
    username = request.user.username
    file_name = name if name is not None else os.path.basename(file.name)
    full_path = _Datastore().save(username, TMP_INPUT_FILE_UPLOAD_DIR, file)
    data_product = _save_data_product(
        request, full_path, name=file_name, content_type=content_type
    )
    return data_product


def copy_input_file(request, data_product):
    path = _get_replica_filepath(data_product)
    name = data_product.productName
    full_path = _Datastore().copy(
        data_product.ownerName,
        path,
        request.user.username,
        TMP_INPUT_FILE_UPLOAD_DIR,
        name=name,
    )
    return _save_copy_of_data_product(request, full_path, data_product)


def is_input_file(request, data_product):
    # Check if file is one of user's files and in TMP_INPUT_FILE_UPLOAD_DIR
    path = _get_replica_filepath(data_product)
    if _Datastore().exists(request.user.username, path):
        rel_path = _Datastore().rel_path(request.user.username, path)
        return os.path.dirname(rel_path) == TMP_INPUT_FILE_UPLOAD_DIR
    else:
        return False


def move_input_file(request, data_product, path):
    source_path = _get_replica_filepath(data_product)
    file_name = data_product.productName
    full_path = _Datastore().move(
        data_product.ownerName, source_path, request.user.username, path, file_name
    )
    _delete_data_product(data_product.ownerName, source_path)
    data_product = _save_copy_of_data_product(request, full_path, data_product)
    return data_product


def move_input_file_from_filepath(
    request, source_path, name=None, content_type=None
):
    "Move a file from filesystem into user's input file staging area."
    username = request.user.username
    file_name = name if name is not None else os.path.basename(source_path)
    full_path = _Datastore().move_external(
        source_path, username, TMP_INPUT_FILE_UPLOAD_DIR, file_name
    )
    data_product = _save_data_product(
        request, full_path, name=file_name, content_type=content_type
    )
    return data_product


def open_file(request, data_product):
    "Return file object for replica if it exists in user storage."
    path = _get_replica_filepath(data_product)
    return _Datastore().open(data_product.ownerName, path)


def exists(request, data_product):
    "Return True if replica for data_product exists in user storage."
    path = _get_replica_filepath(data_product)
    return _Datastore().exists(data_product.ownerName, path)


def dir_exists(request, path):
    "Return True if path exists in user's data store."
    return _Datastore().dir_exists(request.user.username, path)


def user_file_exists(request, path):
    """If file exists, return data product URI, else None."""
    if _Datastore().exists(request.user.username, path):
        full_path = _Datastore().path(request.user.username, path)
        data_product_uri = _get_data_product_uri(request, full_path)
        return data_product_uri
    else:
        return None


def delete_dir(request, path):
    """Delete path in user's data store, if it exists."""
    return _Datastore().delete_dir(request.user.username, path)


def delete(request, data_product):
    "Delete replica for data product in this data store."
    path = _get_replica_filepath(data_product)
    try:
        _Datastore().delete(data_product.ownerName, path)
        _delete_data_product(data_product.ownerName, path)
    except Exception as e:
        logger.exception(
            "Unable to delete file {} for data product uri {}".format(
                path, data_product.productUri
            )
        )
        raise


def listdir(request, path):
    """Return a tuple of two lists, one for directories, the second for files."""
    datastore = _Datastore()
    if datastore.dir_exists(request.user.username, path):
        directories, files = datastore.list_user_dir(request.user.username, path)
        directories_data = []
        for d in directories:
            dpath = os.path.join(path, d)
            created_time = datastore.get_created_time(request.user.username, dpath)
            size = datastore.size(request.user.username, dpath)
            directories_data.append(
                {
                    "name": d,
                    "path": dpath,
                    "created_time": created_time,
                    "size": size,
                    "hidden": dpath == TMP_INPUT_FILE_UPLOAD_DIR,
                }
            )
        files_data = []
        for f in files:
            user_rel_path = os.path.join(path, f)
            created_time = datastore.get_created_time(
                request.user.username, user_rel_path
            )
            size = datastore.size(request.user.username, user_rel_path)
            full_path = datastore.path(request.user.username, user_rel_path)
            data_product_uri = _get_data_product_uri(request, full_path)
            files_data.append(
                {
                    "name": f,
                    "path": user_rel_path,
                    "data-product-uri": data_product_uri,
                    "created_time": created_time,
                    "size": size,
                    "hidden": False,
                }
            )
        return directories_data, files_data
    else:
        raise ObjectDoesNotExist("User storage path does not exist")


def get_experiment_dir(request, project_name=None, experiment_name=None, path=None):
    return _Datastore().get_experiment_dir(
        request.user.username, project_name, experiment_name, path
    )


def create_user_dir(request, path):
    return _Datastore().create_user_dir(request.user.username, path)


def get_rel_path(request, path):
    return _Datastore().rel_path(request.user.username, path)

def _get_data_product_uri(request, full_path):

    from airavata_django_portal_sdk import models
    user_file = models.UserFiles.objects.filter(
        username=request.user.username, file_path=full_path)
    if user_file.exists():
        product_uri = user_file[0].file_dpu
    else:
        data_product = _save_data_product(request, full_path)
        product_uri = data_product.productUri
    return product_uri


def _save_data_product(request, full_path, name=None, content_type=None):
    "Create, register and record in DB a data product for full_path."
    data_product = _create_data_product(
        request.user.username, full_path, name=name, content_type=content_type
    )
    product_uri = _register_data_product(request, full_path, data_product)
    data_product.productUri = product_uri
    return data_product


def _register_data_product(request, full_path, data_product):
    product_uri = request.airavata_client.registerDataProduct(
        request.authz_token, data_product
    )
    from airavata_django_portal_sdk import models
    user_file_instance = models.UserFiles(
        username=request.user.username, file_path=full_path, file_dpu=product_uri
    )
    user_file_instance.save()
    return product_uri


def _save_copy_of_data_product(request, full_path, data_product):
    """Save copy of a data product with a different path."""
    data_product_copy = _copy_data_product(request, data_product, full_path)
    product_uri = _register_data_product(request, full_path, data_product_copy)
    data_product_copy.productUri = product_uri
    return data_product_copy


def _copy_data_product(request, data_product, full_path):
    """Create an unsaved copy of a data product with different path."""
    data_product_copy = copy.copy(data_product)
    data_product_copy.productUri = None
    data_product_copy.ownerName = request.user.username
    data_replica_location = _create_replica_location(
        full_path, data_product_copy.productName
    )
    data_product_copy.replicaLocations = [data_replica_location]
    return data_product_copy


def _delete_data_product(username, full_path):
    # TODO: call API to delete data product from replica catalog when it is
    # available (not currently implemented)
    from airavata_django_portal_sdk import models
    user_file = models.UserFiles.objects.filter(username=username, file_path=full_path)
    if user_file.exists():
        user_file.delete()


def _create_data_product(username, full_path, name=None, content_type=None):
    data_product = DataProductModel()
    data_product.gatewayId = settings.GATEWAY_ID
    data_product.ownerName = username
    if name is not None:
        file_name = name
    else:
        file_name = os.path.basename(full_path)
    data_product.productName = file_name
    data_product.dataProductType = DataProductType.FILE
    final_content_type = _determine_content_type(full_path, content_type)
    if final_content_type is not None:
        data_product.productMetadata = {"mime-type": final_content_type}
    data_replica_location = _create_replica_location(full_path, file_name)
    data_product.replicaLocations = [data_replica_location]
    return data_product


def _determine_content_type(full_path, content_type=None):
    result = content_type
    if result is None:
        # Try to guess the content-type from file extension
        guessed_type, encoding = mimetypes.guess_type(full_path)
        result = guessed_type
    if result is None or result == "application/octet-stream":
        # Check if file is Unicode text by trying to read some of it
        try:
            open(full_path, "r").read(1024)
            result = "text/plain"
        except UnicodeDecodeError:
            logger.debug(f"Failed to read as Unicode text: {full_path}")
    return result


def _create_replica_location(full_path, file_name):
    data_replica_location = DataReplicaLocationModel()
    data_replica_location.storageResourceId = settings.GATEWAY_DATA_STORE_RESOURCE_ID
    data_replica_location.replicaName = "{} gateway data store copy".format(file_name)
    data_replica_location.replicaLocationCategory = (
        ReplicaLocationCategory.GATEWAY_DATA_STORE
    )
    data_replica_location.replicaPersistentType = ReplicaPersistentType.TRANSIENT
    data_replica_location.filePath = "file://{}:{}".format(
        settings.GATEWAY_DATA_STORE_HOSTNAME, full_path
    )
    return data_replica_location


def _get_replica_filepath(data_product):
    replica_filepaths = [
        rep.filePath
        for rep in data_product.replicaLocations
        if rep.replicaLocationCategory == ReplicaLocationCategory.GATEWAY_DATA_STORE
    ]
    replica_filepath = replica_filepaths[0] if len(replica_filepaths) > 0 else None
    if replica_filepath:
        return urlparse(replica_filepath).path
    return None


class _Datastore:
    """Internal datastore abstraction."""


    def __init__(self, directory=None):
        if directory:
            self.directory = directory
        else:
            self.directory = settings.GATEWAY_DATA_STORE_DIR

    def exists(self, username, path):
        """Check if file path exists in this data store."""
        try:
            return self._user_data_storage(username).exists(path) and os.path.isfile(
                self.path(username, path)
            )
        except SuspiciousFileOperation as e:
            logger.warning("Invalid path for user {}: {}".format(username, str(e)))
            return False

    def dir_exists(self, username, path):
        """Check if directory path exists in this data store."""
        try:
            return self._user_data_storage(username).exists(path) and os.path.isdir(
                self.path(username, path)
            )
        except SuspiciousFileOperation as e:
            logger.warning("Invalid path for user {}: {}".format(username, str(e)))
            return False

    def open(self, username, path):
        """Open path for user if it exists in this data store."""
        if self.exists(username, path):
            return self._user_data_storage(username).open(path)
        else:
            raise ObjectDoesNotExist("File path does not exist: {}".format(path))

    def save(self, username, path, file, name=None):
        """Save file to username/path in data store."""
        # file.name may be full path, so get just the name of the file
        file_name = name if name is not None else os.path.basename(file.name)
        user_data_storage = self._user_data_storage(username)
        file_path = os.path.join(path, user_data_storage.get_valid_name(file_name))
        input_file_name = user_data_storage.save(file_path, file)
        input_file_fullpath = user_data_storage.path(input_file_name)
        return input_file_fullpath

    def move(
        self, source_username, source_path, target_username, target_dir, file_name
    ):
        source_full_path = self.path(source_username, source_path)
        user_data_storage = self._user_data_storage(target_username)
        # Make file_name a valid filename
        target_path = os.path.join(
            target_dir, user_data_storage.get_valid_name(file_name)
        )
        # Get available file path: if there is an existing file at target_path
        # create a uniquely named path
        target_path = user_data_storage.get_available_name(target_path)
        target_full_path = self.path(target_username, target_path)
        file_move_safe(source_full_path, target_full_path)
        return target_full_path

    def move_external(self, external_path, target_username, target_dir, file_name):
        user_data_storage = self._user_data_storage(target_username)
        # Make file_name a valid filename
        target_path = os.path.join(
            target_dir, user_data_storage.get_valid_name(file_name)
        )
        # Get available file path: if there is an existing file at target_path
        # create a uniquely named path
        target_path = user_data_storage.get_available_name(target_path)
        if not self.dir_exists(target_username, target_dir):
            self.create_user_dir(target_username, target_dir)
        target_full_path = self.path(target_username, target_path)
        file_move_safe(external_path, target_full_path)
        return target_full_path

    def create_user_dir(self, username, path):
        user_data_storage = self._user_data_storage(username)
        if not user_data_storage.exists(path):
            self._makedirs(username, path)
        else:
            raise Exception("Directory {} already exists".format(path))

    def copy(
        self, source_username, source_path, target_username, target_path, name=None
    ):
        """Copy a user file into target_path dir."""
        f = self.open(source_username, source_path)
        return self.save(target_username, target_path, f, name=name)

    def delete(self, username, path):
        """Delete file in this data store."""
        if self.exists(username, path):
            user_data_storage = self._user_data_storage(username)
            user_data_storage.delete(path)
        else:
            raise ObjectDoesNotExist("File path does not exist: {}".format(path))

    def delete_dir(self, username, path):
        """Delete entire directory in this data store."""
        if self.dir_exists(username, path):
            user_path = self.path(username, path)
            shutil.rmtree(user_path)
        else:
            raise ObjectDoesNotExist("File path does not exist: {}".format(path))

    def get_experiment_dir(
        self, username, project_name=None, experiment_name=None, path=None
    ):
        """Return an experiment directory (full path) for the given experiment."""
        user_experiment_data_storage = self._user_data_storage(username)
        if path is None:
            proj_dir_name = user_experiment_data_storage.get_valid_name(project_name)
            # AIRAVATA-3245 Make project directory with correct permissions
            if not user_experiment_data_storage.exists(proj_dir_name):
                self._makedirs(username, proj_dir_name)
            experiment_dir_name = os.path.join(
                proj_dir_name,
                user_experiment_data_storage.get_valid_name(experiment_name),
            )
            # Since there may already be another experiment with the same name in
            # this project, we need to check for available name
            experiment_dir_name = user_experiment_data_storage.get_available_name(
                experiment_dir_name
            )
            experiment_dir = user_experiment_data_storage.path(experiment_dir_name)
        else:
            # path can be relative to the user's storage space or absolute (as long
            # as it is still inside the user's storage space)
            # if path is passed in, assumption is that it has already been created
            user_experiment_data_storage = self._user_data_storage(username)
            experiment_dir = user_experiment_data_storage.path(path)
        if not user_experiment_data_storage.exists(experiment_dir):
            self._makedirs(username, experiment_dir)
        return experiment_dir

    def _makedirs(self, username, dir_path):
        user_experiment_data_storage = self._user_data_storage(username)
        full_path = user_experiment_data_storage.path(dir_path)
        os.makedirs(
            full_path, mode=user_experiment_data_storage.directory_permissions_mode
        )
        # os.makedirs mode isn't always respected so need to chmod to be sure
        os.chmod(
            full_path, mode=user_experiment_data_storage.directory_permissions_mode
        )

    def list_user_dir(self, username, file_path):
        logger.debug("file_path={}".format(file_path))
        user_data_storage = self._user_data_storage(username)
        return user_data_storage.listdir(file_path)

    def get_created_time(self, username, file_path):
        user_data_storage = self._user_data_storage(username)
        return user_data_storage.get_created_time(file_path)

    def size(self, username, file_path):
        user_data_storage = self._user_data_storage(username)
        full_path = self.path(username, file_path)
        if os.path.isdir(full_path):
            return self._get_dir_size(full_path)
        else:
            return user_data_storage.size(file_path)

    def path(self, username, file_path):
        user_data_storage = self._user_data_storage(username)
        return user_data_storage.path(file_path)

    def rel_path(self, username, file_path):
        full_path = self.path(username, file_path)
        return os.path.relpath(full_path, self.path(username, ""))

    def _user_data_storage(self, username):
        return FileSystemStorage(location=os.path.join(self.directory, username))

    # from https://stackoverflow.com/a/1392549
    def _get_dir_size(self, start_path="."):
        total_size = 0
        for dirpath, dirnames, filenames in os.walk(start_path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                # Check for broken symlinks (.exists return False for broken
                # symlinks)
                if os.path.exists(fp):
                    total_size += os.path.getsize(fp)
        return total_size
