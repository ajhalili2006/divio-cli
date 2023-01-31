import itertools
import json
import os
import sys

import click
import sentry_sdk
from click_aliases import ClickAliasedGroup
from sentry_sdk.integrations.atexit import AtexitIntegration

import divio_cli

from . import exceptions, localdev, messages, settings
from .check_system import check_requirements, check_requirements_human
from .cloud import CloudClient, get_endpoint
from .excepthook import DivioExcepthookIntegration, divio_shutdown
from .localdev.utils import allow_remote_id_override
from .upload.addon import upload_addon
from .upload.boilerplate import upload_boilerplate
from .utils import (
    Map,
    echo_large_content,
    get_cp_url,
    get_git_checked_branch,
    hr,
    launch_url,
    open_application_cloud_site,
    table,
)
from .validators.addon import validate_addon
from .validators.boilerplate import validate_boilerplate


try:
    import ipdb as pdb
except ImportError:
    import pdb


@click.group(
    cls=ClickAliasedGroup,
    context_settings={"help_option_names": ["--help", "-h"]},
)
@click.option(
    "-d",
    "--debug/--no-debug",
    default=False,
    help="Drop into the debugger if command execution raises an exception.",
)
@click.option(
    "-p/-P",
    "--pager/--no-pager",
    default=False,
    is_flag=True,
    help="Choose whether to display content via pager or not. Leave blank for no pager.",
)
@click.option(
    "-z",
    "--zone",
    default=None,
    help="Specify the Divio zone. Defaults to divio.com.",
)
@click.option(
    "-s",
    "--sudo",
    default=False,
    is_flag=True,
    help="Run as sudo?",
    hidden=True,
)
@click.pass_context
def cli(ctx, debug, pager, zone, sudo):
    if sudo:
        click.secho("Running as sudo", fg="yellow")

    ctx.obj = Map()
    ctx.obj.client = CloudClient(
        get_endpoint(zone=zone), debug=debug, sudo=sudo
    )
    ctx.obj.zone = zone
    ctx.obj.pager = pager

    if debug:

        def exception_handler(type, value, traceback):
            click.secho(
                "\nAn exception occurred while executing the requested "
                "command:",
                fg="red",
                err=True,
            )
            hr(
                fg="red",
                err=True,
            )
            sys.__excepthook__(type, value, traceback)
            click.secho(
                "\nStarting interactive debugging session:", fg="red", err=True
            )
            hr(
                fg="red",
                err=True,
            )
            pdb.post_mortem(traceback)

        sys.excepthook = exception_handler
    else:
        sentry_sdk.init(
            ctx.obj.client.config.get_sentry_dsn(),
            traces_sample_rate=0,
            release=divio_cli.__version__,
            server_name="client",
            integrations=[
                DivioExcepthookIntegration(),
                AtexitIntegration(callback=divio_shutdown),
            ],
        )

    try:
        is_version_command = sys.argv[1] == "version"
    except IndexError:
        is_version_command = False

    # skip if 'divio version' is run
    if not is_version_command:
        # check for newer versions
        update_info = ctx.obj.client.config.check_for_updates()
        if update_info["update_available"]:
            click.secho(
                "New version {} is available. Type `divio version` to "
                "show information about upgrading.".format(
                    update_info["remote"]
                ),
                fg="yellow",
                err=True,
            )


def login_token_helper(ctx, value):
    if not value:
        url = ctx.obj.client.get_access_token_url()
        click.secho("Your browser has been opened to visit: {}".format(url))
        launch_url(url)
        value = click.prompt(
            "Please copy the access token and paste it here. (your input is not displayed)",
            hide_input=True,
        )

    # Detect pasting shortcut malfunction (Windows users)
    # When this shortcut is disabled then the character \x16
    # (which will appear as ^V) is generated by the terminal when trying to use it.
    if "".join(set(value)) == "\x16":
        click.secho(
            "\nThe access token provided indicates a copy/paste malfunction.\nRead more here: https://r.divio.com/divio-login-windows-users.",
            fg="yellow",
        )
    return value


@cli.command()
@click.argument("token", required=False)
@click.option(
    "--check",
    is_flag=True,
    default=False,
    help="Check for current login status.",
)
@click.pass_context
def login(ctx, token, check):
    """Authorise your machine with the Divio Control Panel."""
    success = True
    if check:
        success, msg = ctx.obj.client.check_login_status()
    else:
        token = login_token_helper(ctx, token)
        msg = ctx.obj.client.login(token)

    click.echo(msg)
    sys.exit(0 if success else 1)


@cli.group(cls=ClickAliasedGroup, aliases=["project"])
def app():
    """Manage your application"""


@app.command(name="list")
@click.option(
    "-g",
    "--grouped",
    is_flag=True,
    default=False,
    help="Group by organisation.",
)
@click.option(
    "-p/-P",
    "--pager/--no-pager",
    default=False,
    is_flag=True,
    help="Choose whether to display content via pager or not. Leave blank for no pager.",
)
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_obj
def application_list(obj, grouped, pager, as_json):
    """List all your applications."""
    obj.pager = pager
    api_response = obj.client.get_applications()

    if as_json:
        click.echo(json.dumps(api_response, indent=2, sort_keys=True))
        return

    header = ("ID", "Slug", "Name", "Organisation")

    # get all users + organisations
    groups = {
        "users": {
            account["id"]: {"name": "Personal", "applications": []}
            for account in api_response["accounts"]
            if account["type"] == "user"
        },
        "organisations": {
            account["id"]: {"name": account["name"], "applications": []}
            for account in api_response["accounts"]
            if account["type"] == "organisation"
        },
    }

    # sort websites into groups
    for website in api_response["websites"]:
        organisation_id = website["organisation_id"]
        if organisation_id:
            owner = groups["organisations"][website["organisation_id"]]
        else:
            owner = groups["users"][website["owner_id"]]
        owner["applications"].append(
            (str(website["id"]), website["domain"], website["name"])
        )

    accounts = itertools.chain(
        groups["users"].items(), groups["organisations"].items()
    )

    def sort_applications(items):
        return sorted(items, key=lambda x: x[0].lower())

    if grouped:
        output_items = []
        for group, data in accounts:
            applications = data["applications"]
            if applications:
                output_items.append(
                    "{title}\n{line}\n\n{table}\n\n".format(
                        title=data["name"],
                        line="=" * len(data["name"]),
                        table=table(
                            sort_applications(applications), header[:3]
                        ),
                    )
                )
        output = os.linesep.join(output_items).rstrip(os.linesep)
    else:
        # add account name to all applications
        applications = [
            each + (data["name"],)
            for group, data in accounts
            for each in data["applications"]
        ]
        output = table(sort_applications(applications), header)

    echo_large_content(output, ctx=obj)


@app.command(name="deploy")
@click.argument("environment", default="test")
@allow_remote_id_override
@click.pass_obj
def application_deploy(obj, remote_id, environment):
    """Deploy application."""
    obj.client.deploy_application_or_get_progress(remote_id, environment)


@app.command(name="deploy-log")
@click.argument("environment", default="test")
@allow_remote_id_override
@click.pass_obj
def application_deploy_log(obj, remote_id, environment):
    """View last deployment log."""
    deploy_log = obj.client.get_deploy_log(remote_id, environment)
    if deploy_log:
        echo_large_content(deploy_log, ctx=obj)
    else:
        click.secho(
            "No logs available.",
            fg="yellow",
        )


@app.command(name="logs")
@click.argument("environment", default="test")
@click.option(
    "--tail", "tail", default=False, is_flag=True, help="Tail the output."
)
@click.option(
    "--utc", "utc", default=False, is_flag=True, help="Show times in UTC/"
)
@allow_remote_id_override
@click.pass_obj
def application_logs(obj, remote_id, environment, tail, utc):
    """View logs."""
    obj.client.show_log(remote_id, environment, tail, utc)


@app.command(name="ssh")
@click.argument("environment", default="test")
@allow_remote_id_override
@click.pass_obj
def application__ssh(obj, remote_id, environment):
    """Establish SSH connection."""
    obj.client.ssh(remote_id, environment)


@app.command(name="configure")
@click.pass_obj
def configure(obj):
    """Associate a local application with a Divio cloud applications."""
    localdev.configure(client=obj.client, zone=obj.zone)


@app.command(name="dashboard")
@allow_remote_id_override
@click.pass_obj
def application_dashboard(obj, remote_id):
    """Open the application dashboard on the Divio Control Panel."""
    launch_url(get_cp_url(client=obj.client, application_id=remote_id))


@app.command(name="up", aliases=["start"])
def application_up():
    """Start the local application (equivalent to: docker-compose up)."""
    localdev.start_application()


@app.command(name="down", aliases=["stop"])
def application_down():
    """Stop the local application."""
    localdev.stop_application()


@app.command(name="open")
@click.argument("environment", default="")
@allow_remote_id_override
@click.pass_obj
def application_open(obj, remote_id, environment):
    """Open local or cloud applications in a browser."""
    if environment:
        open_application_cloud_site(
            obj.client, application_id=remote_id, environment=environment
        )
    else:
        localdev.open_application()


@app.command(name="update")
@click.option(
    "--strict",
    "strict",
    default=False,
    is_flag=True,
    help="A strict update will fail on a warning.",
)
@click.pass_obj
def application_update(obj, strict):
    """Update the local application with new code changes, then build it.

    Runs:

    git pull
    docker-compose pull
    docker-compose build
    docker-compose run web start migrate"""

    localdev.update_local_application(
        get_git_checked_branch(), client=obj.client, strict=strict
    )


@app.command(name="deployments")
@click.option(
    "-s",
    "--stage",
    "-e",
    "--environment",
    "environment",
    # This should never conflict with an actual environment slug
    # as it is not permitted to be blank in the first place.
    default="",
    type=str,
    help=(
        "Choose a specific environment (by name) from which deployments "
        "will be collected or leave blank for all environments."
    ),
)
@click.option(
    "-d",
    "--deployment",
    default="",
    type=str,
    help="Retrieve the details of a specific deployment by providing it's uuid.",
)
@click.option(
    "-g",
    "--get-var",
    type=str,
    help=(
        "Retrieve a specific environment variable by providing it's name. "
        "A deployment must be provided as well."
    ),
)
@click.option(
    "-p/-P",
    "--pager/--no-pager",
    default=False,
    is_flag=True,
    help="Choose whether to display content via pager or not. Leave blank for no pager.",
)
@click.option("--json", "as_json", is_flag=True, default=False)
@allow_remote_id_override
@click.pass_obj
def deployments(
    obj,
    remote_id,
    environment,
    deployment,
    get_var,
    pager,
    as_json,
):
    """Retrieve deployments."""
    environment = environment.lower()
    obj.pager = pager

    results = obj.client.get_deployments(
        website_id=remote_id,
        environment=environment,
        deployment=deployment,
        get_var=get_var,
    )

    if as_json:
        json_content = json.dumps(results, indent=2)
        echo_large_content(json_content, ctx=obj)
    else:
        if get_var:
            # All necessary checks have been made in cloud.py. By now a single
            # environment variable is returned or not and exited properly.
            var = results[0]
            content_table_title = f"Environment: {var['environment']} ({var['environment_uuid']})"
            echo_large_content(
                content_table_title
                + "\n"
                + table(
                    [[var["name"], var["value"]]],
                    [
                        "name",
                        "value",
                    ],
                    tablefmt="grid",
                ),
                ctx=obj,
            )
        # Displaying a single or multiple deployments as tables.
        else:
            content_tables = ""
            # Deployments in table format display less content that in
            # json format. Here are the desired columns to be displayed.
            columns = [
                "uuid",
                "author",
                "status",
                "is_usable",
                "success",
            ]
            # Single deployment.
            if deployment:
                # All necessary checks have been made in cloud.py. By now a single
                # deployment is returned or not.
                dep = results[0]
                environment_slug = dep["environment"]
                environment_uuid = dep["environment_uuid"]
                content_table_title = (
                    f"Environment: {environment_slug} ({environment_uuid})"
                )
                content_tables = (
                    content_table_title
                    + "\n"
                    + table(
                        [[dep[key] for key in columns]],
                        columns,
                        tablefmt="grid",
                    )
                )
            # Listing deployments.
            else:
                for result in results:
                    environment_slug = result["environment"]
                    environment_uuid = result["environment_uuid"]
                    content_table_title = (
                        f"Environment: {environment_slug} ({environment_uuid})"
                    )

                    rows = [
                        [row[key] for key in columns]
                        for row in result["deployments"]
                    ]
                    content_table = (
                        content_table_title
                        + "\n"
                        + table(rows, columns, tablefmt="grid")
                        + "\n" * 3
                    )
                    content_tables += content_table
            echo_large_content(content_tables.strip("\n"), ctx=obj)


@app.command(name="environment-variables", aliases=["env-vars"])
@click.option(
    "-s",
    "--stage",
    "-e",
    "--environment",
    "environment",
    # This should never conflict with an actual environment slug
    # as it is not permitted to be blank in the first place.
    default="",
    type=str,
    help=(
        "Choose a specific environment (by name) from which the environment variables "
        "will be collected or leave blank for all environments."
    ),
)
@click.option(
    "-p/-P",
    "--pager/--no-pager",
    default=False,
    is_flag=True,
    help="Choose whether to display content via pager or not. Leave blank for no pager.",
)
@click.option(
    "-g",
    "--get-var",
    type=str,
    help="Retrieve a specific environment variable by providing it's name.",
)
@click.option("--json", "as_json", is_flag=True, default=False)
@allow_remote_id_override
@click.pass_obj
def environment_variables(
    obj,
    remote_id,
    environment,
    pager,
    get_var,
    as_json,
):
    """Retrieve environment variables."""
    environment = environment.lower()
    obj.pager = pager

    results = obj.client.get_environment_variables(
        website_id=remote_id,
        environment=environment,
    )

    if as_json:
        if get_var:
            content = []
            for block in results:
                for environment_variable in block["environment_variables"]:
                    if environment_variable["name"] == get_var:
                        content.append(
                            {
                                "environment": block["environment"],
                                "environment_uuid": block["environment_uuid"],
                                "environment_variables": [
                                    environment_variable
                                ],
                            }
                        )
                        break
            json_content = json.dumps(content, indent=2)
            if content:
                echo_large_content(json_content, ctx=obj)
            else:
                click.secho(
                    f"Could not find any environment variable named {get_var}.",
                    fg="yellow",
                )
        else:
            json_content = json.dumps(results, indent=2)
            echo_large_content(json_content, ctx=obj)

    # Display results as tables
    else:
        if get_var:
            content_tables = ""
            for block in results:
                for environment_variable in block["environment_variables"]:
                    if environment_variable["name"] == get_var:
                        content_table_title = "Environment: {} ({})".format(
                            block["environment"], block["environment_uuid"]
                        )
                        value = (
                            # None is necessary for sensitive environment variables where the value
                            # is not included in the response.
                            None
                            if "value" not in environment_variable.keys()
                            else environment_variable["value"]
                        )
                        is_sensitive = environment_variable["is_sensitive"]
                        content_table = (
                            content_table_title
                            + "\n"
                            + table(
                                [[get_var, value, is_sensitive]],
                                ["name", "value", "is_sensitive"],
                                tablefmt="grid",
                            )
                            + "\n" * 3
                        )
                        content_tables += content_table
                        break

            if content_tables:
                echo_large_content(content_tables.strip("\n"), ctx=obj)
            else:
                click.secho(
                    f"Could not find any environment variable named {get_var}.",
                    fg="yellow",
                )
        # Display results as tables and no specific
        # environment variable was requested (by name).
        else:
            content_tables = ""
            for result in results:
                environment_slug = result["environment"]
                environment_uuid = result["environment_uuid"]
                content_table_title = (
                    f"Environment: {environment_slug} ({environment_uuid})"
                )
                columns = ["name", "value", "is_sensitive"]
                # None is necessary for sensitive environment variables where the value
                # is not included in the response.
                rows = [
                    [
                        row[key] if key in row.keys() else None
                        for key in columns
                    ]
                    for row in result["environment_variables"]
                ]
                content_table = (
                    content_table_title
                    + "\n"
                    + table(rows, columns, tablefmt="grid")
                    + "\n" * 3
                )
                content_tables += content_table
            echo_large_content(content_tables.strip("\n"), ctx=obj)


@app.command(name="status")
def app_status():
    """Show local application status."""
    localdev.show_application_status()


@app.command(name="setup")
@click.argument("slug")
@click.option(
    "-s",
    "--stage",
    "-e",
    "--environment",
    "environment",
    default="test",
    help="Specify environment from which media and content data will be pulled.",
)
@click.option(
    "-p",
    "--path",
    default=".",
    help="Install application in path.",
    type=click.Path(writable=True, readable=True),
)
@click.option(
    "--overwrite",
    is_flag=True,
    default=False,
    help="Overwrite the application directory if it already exists.",
)
@click.option(
    "--skip-doctor",
    is_flag=True,
    default=False,
    help="Skip system test before setting up the application.",
)
@click.pass_obj
def application_setup(obj, slug, environment, path, overwrite, skip_doctor):
    """Set up a development environment for a Divio application."""
    if not skip_doctor and not check_requirements_human(
        config=obj.client.config, silent=True
    ):
        click.secho(
            "There was a problem while checking your system. Please run "
            "'divio doctor'.",
            fg="red",
            err=True,
        )
        sys.exit(1)

    localdev.create_workspace(
        obj.client, slug, environment, path, overwrite, obj.zone
    )


@app.group(name="pull")
def application_pull():
    """Pull db or files from the Divio cloud environment."""


@application_pull.command(name="db")
@click.option(
    "--keep-tempfile",
    is_flag=True,
    default=False,
    help="Keep the temporary file with the data.",
)
@click.argument("environment", default="test")
@click.argument("prefix", default=localdev.DEFAULT_SERVICE_PREFIX)
@allow_remote_id_override
@click.pass_obj
def pull_db(obj, remote_id, environment, prefix, keep_tempfile):
    """
    Pull database the Divio cloud environment.
    """
    from .localdev import utils

    application_home = utils.get_application_home()
    db_type = utils.get_db_type(prefix, path=application_home)
    dump_path = os.path.join(application_home, settings.DIVIO_DUMP_FOLDER)

    localdev.ImportRemoteDatabase(
        client=obj.client,
        environment=environment,
        prefix=prefix,
        remote_id=remote_id,
        db_type=db_type,
        dump_path=dump_path,
        keep_tempfile=keep_tempfile,
    )()


@application_pull.command(name="media")
@click.argument("environment", default="test")
@allow_remote_id_override
@click.pass_obj
def pull_media(obj, remote_id, environment):
    """
    Pull media files from the Divio cloud environment.
    """
    localdev.pull_media(
        obj.client, environment=environment, remote_id=remote_id
    )


@app.group(name="push")
def application_push():
    """Push db or media files to the Divio cloud environment."""


@application_push.command(name="db")
@click.argument("environment", default="test")
@click.option(
    "-d",
    "--dumpfile",
    default=None,
    type=click.Path(exists=True),
    help="Specify a dumped database file to upload.",
)
@click.option(
    "--noinput",
    is_flag=True,
    default=False,
    help="Don't ask for confirmation.",
)
@click.argument("prefix", default=localdev.DEFAULT_SERVICE_PREFIX)
@allow_remote_id_override
@click.pass_obj
def push_db(obj, remote_id, prefix, environment, dumpfile, noinput):
    """
    Push database to the Divio cloud environment..
    """
    from .localdev import utils

    application_home = utils.get_application_home()
    db_type = utils.get_db_type(prefix, path=application_home)
    if not dumpfile:
        if not noinput:
            click.secho(
                messages.PUSH_DB_WARNING.format(environment=environment),
                fg="red",
            )
            if not click.confirm("\nAre you sure you want to continue?"):
                return
        localdev.push_db(
            client=obj.client,
            environment=environment,
            remote_id=remote_id,
            prefix=prefix,
            db_type=db_type,
        )
    else:
        if not noinput:
            click.secho(
                messages.PUSH_DB_WARNING.format(environment=environment),
                fg="red",
            )
            if not click.confirm("\nAre you sure you want to continue?"):
                return
        localdev.push_local_db(
            obj.client,
            environment=environment,
            dump_filename=dumpfile,
            website_id=remote_id,
            prefix=prefix,
        )


@application_push.command(name="media")
@click.argument("environment", default="test")
@click.option(
    "--noinput",
    is_flag=True,
    default=False,
    help="Don't ask for confirmation.",
)
@click.argument("prefix", default=localdev.DEFAULT_SERVICE_PREFIX)
@allow_remote_id_override
@click.pass_obj
def push_media(obj, remote_id, prefix, environment, noinput):
    """
    Push database to the Divio cloud environment..
    """

    if not noinput:
        click.secho(
            messages.PUSH_MEDIA_WARNING.format(environment=environment),
            fg="red",
        )
        if not click.confirm("\nAre you sure you want to continue?"):
            return
    localdev.push_media(
        obj.client, environment=environment, remote_id=remote_id, prefix=prefix
    )


@app.group(name="import")
def application_import():
    """Import local database dump."""


@application_import.command(name="db")
@click.argument("prefix", default=localdev.DEFAULT_SERVICE_PREFIX)
@click.argument(
    "dump-path",
    default=localdev.DEFAULT_DUMP_FILENAME,
    type=click.Path(exists=True),
)
@click.pass_obj
def import_db(obj, dump_path, prefix):
    """
    Load a database dump into your local database.
    """
    from .localdev import utils

    application_home = utils.get_application_home()
    db_type = utils.get_db_type(prefix, path=application_home)
    localdev.ImportLocalDatabase(
        client=obj.client,
        custom_dump_path=dump_path,
        prefix=prefix,
        db_type=db_type,
    )()


@app.group(name="export")
def application_export():
    """Export local database dump."""


@application_export.command(name="db")
@click.argument("prefix", default=localdev.DEFAULT_SERVICE_PREFIX)
def export_db(prefix):
    """
    Export a dump of your local database
    """
    localdev.export_db(prefix=prefix)


@app.command(name="develop")
@click.argument("package")
@click.option(
    "--no-rebuild",
    is_flag=True,
    default=False,
    help="Do not rebuild docker container automatically.",
)
def application_develop(package, no_rebuild):
    """Add a package 'package' to your local application environment."""
    localdev.develop_package(package, no_rebuild)


@cli.group()
@click.option("-p", "--path", default=".", help="Addon directory")
@click.pass_obj
def addon(obj, path):
    """Validate and upload addons packages to the Divio cloud."""


@addon.command(name="validate")
@click.pass_context
def addon_validate(ctx):
    """Validate addon configuration."""
    try:
        validate_addon(ctx.parent.params["path"])
    except exceptions.DivioException as exc:
        raise click.ClickException(*exc.args)
    click.echo("Addon is valid!")


@addon.command(name="upload")
@click.pass_context
def addon_upload(ctx):
    """Upload addon to the Divio Control Panel."""
    try:
        ret = upload_addon(ctx.obj.client, ctx.parent.params["path"])
    except exceptions.DivioException as exc:
        raise click.ClickException(*exc.args)
    click.echo(ret)


@addon.command(name="register")
@click.argument("verbose_name")
@click.argument("package_name")
@click.option(
    "-o",
    "--organisation",
    help="Register an addon for an organisation.",
    type=int,
)
@click.pass_context
def addon_register(ctx, package_name, verbose_name, organisation):
    """Register your addon on the Divio Control Panel\n
    - Verbose Name:        Name of the Addon as it appears in the Marketplace
    - Package Name:        System wide unique Python package name
    """
    ret = ctx.obj.client.register_addon(
        package_name, verbose_name, organisation
    )
    click.echo(ret)


@cli.group()
@click.option("-p", "--path", default=".", help="Boilerplate directory")
@click.pass_obj
def boilerplate(obj, path):
    """Validate and upload boilerplate packages to the Divio cloud."""


@boilerplate.command(name="validate")
@click.pass_context
def boilerplate_validate(ctx):
    """Validate boilerplate configuration."""
    try:
        validate_boilerplate(ctx.parent.params["path"])
    except exceptions.DivioException as exc:
        raise click.ClickException(*exc.args)
    click.echo("Boilerplate is valid.")


@boilerplate.command(name="upload")
@click.option(
    "--noinput",
    is_flag=True,
    default=False,
    help="Don't ask for confirmation.",
)
@click.pass_context
def boilerplate_upload(ctx, noinput):
    """Upload boilerplate to the Divio Control Panel."""
    try:
        ret = upload_boilerplate(
            ctx.obj.client, ctx.parent.params["path"], noinput
        )
    except exceptions.DivioException as exc:
        raise click.ClickException(*exc.args)
    click.echo(ret)


@cli.command()
@click.option(
    "-s",
    "--skip-check",
    is_flag=True,
    default=False,
    help="Don't check PyPI for newer version.",
)
@click.option("-m", "--machine-readable", is_flag=True, default=False)
@click.pass_obj
def version(obj, skip_check, machine_readable):
    """Show version info."""
    if skip_check:
        from . import __version__

        update_info = {"current": __version__}
    else:
        update_info = obj.client.config.check_for_updates(force=True)

    update_info["location"] = os.path.dirname(os.path.realpath(sys.executable))

    if machine_readable:
        click.echo(json.dumps(update_info))
    else:
        click.echo(
            "divio-cli {} from {}\n".format(
                update_info["current"], update_info["location"]
            )
        )

        if not skip_check:
            if update_info["update_available"]:
                click.secho(
                    "New version {version} is available. Upgrade options:\n\n"
                    " - Using pip\n"
                    "   pip install --upgrade divio-cli\n\n"
                    " - Download the latest release from GitHub\n"
                    "   https://github.com/divio/divio-cli/releases".format(
                        version=update_info["remote"]
                    ),
                    fg="yellow",
                )
            elif update_info["pypi_error"]:
                click.secho(
                    "There was an error while trying to check for the latest "
                    "version on pypi.python.org:\n"
                    "{}".format(update_info["pypi_error"]),
                    fg="red",
                    err=True,
                )
            else:
                click.echo("You have the latest version of divio-cli.")


@cli.command()
@click.option("-m", "--machine-readable", is_flag=True, default=False)
@click.option("-c", "--checks", default=None)
@click.pass_obj
def doctor(obj, machine_readable, checks):
    """Check that your system meets the development requirements.

    To disable checks selectively in case of false positives, see
    https://docs.divio.com/en/latest/reference/divio-cli/#using-skip-doctor-checks"""

    if checks:
        checks = checks.split(",")

    if machine_readable:
        errors = {
            check: error
            for check, check_name, error in check_requirements(
                obj.client.config, checks
            )
        }
        exitcode = 1 if any(errors.values()) else 0
        click.echo(json.dumps(errors), nl=False)
    else:
        click.echo("Verifying your system setup...")
        exitcode = (
            0 if check_requirements_human(obj.client.config, checks) else 1
        )

    sys.exit(exitcode)
