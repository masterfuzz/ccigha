import os
import sys
import ruamel.yaml

ruamel.yaml.representer.RoundTripRepresenter.ignore_aliases = lambda x, y: True
from ruamel.yaml import YAML
import re

SSH_ACTION = [
    {
        "uses": "./.github/actions/configure-ssh",
        "with": {"ssh-private-key": "${{ secrets.TETRATE_CI_SSH_PRIVATE_KEY }}"},
    },
]


def replace_parameters(string, parameters) -> str:
    if "<<" not in string:
        return string

    def replacer(m: re.Match):
        ident = m.group(1).strip().split(".")
        if ident[0] == "pipeline":
            return parameters["pipeline"][ident[-1]]
        return parameters[ident[-1]]

    return re.sub(r"<<(.*)>>", replacer, string)


def fix_path(path: str) -> str:
    return path.replace("~", "..")


def image_map(image: str) -> str:
    repo, tag = image.split(":")
    match repo:
        case "cimg/go":
            return "golang:" + tag
        case _ if "cimg/" in repo:
            return repo.split("/")[1] + ":" + tag
        case _:
            # print(f"warn: {repo}:{tag}")
            return image


class Converter:
    def __init__(self, cci_pipeline) -> None:
        self.cci_pipeline = cci_pipeline
        self.cci_orbs = self.cci_pipeline["orbs"]
        self.cci_workflows = self.cci_pipeline["workflows"]
        self.cci_commands = self.cci_pipeline["commands"]
        self.cci_job_templates = self.cci_pipeline["jobs"]
        self.cci_executors = self.cci_pipeline["executors"]

    @staticmethod
    def load(fh):
        return Converter(YAML(typ="safe", pure=True).load(fh))

    def export(self, github_directory, filter=None):
        print(
            f"Writing converting commands to actions and writing to {github_directory}/actions"
        )
        for command in self.cci_commands:
            self.write_command(command, github_directory)

        for wf_name, workflow in self.cci_workflows.items():
            if wf_name == "version":
                continue
            print(f"Converting workflow {wf_name}")
            gh_workflow = self.convert_workflow(wf_name, workflow)

            if filter:
                gh_workflow["jobs"] = {
                    name: job
                    for name, job in gh_workflow["jobs"].items()
                    if name in filter
                }
            fpath = os.path.join(
                github_directory,
                "workflows",
                f"gha_migration_{'filtered_' if filter else ''}{wf_name}.yaml",
            )
            print(f"write workflow with {len(gh_workflow['jobs'])} jobs to {fpath}")
            with open(
                fpath,
                "w",
            ) as fh:
                YAML().dump(gh_workflow, fh)

    def convert_workflow(self, wf_name, workflow):
        parameters = {
            key: value.get("default", "")
            for key, value in self.cci_pipeline["parameters"].items()
        }

        gh_jobs = {}
        gh_workflow = {
            "name": f"CircleCI Generated workflow for {wf_name.capitalize()}",
            "on": {
                "pull_request": {"branches": ["master"]},
                "workflow_dispatch": {},
            },
            "permissions": {"contents": "read"},
            "jobs": gh_jobs,
        }

        for cci_job in workflow["jobs"]:
            name, gh_job = self.convert_job(cci_job, parameters)
            gh_jobs[name] = gh_job

        return gh_workflow

    def convert_job(self, cci_job, parameters):
        template_name = list(cci_job.keys())[0]
        cci_job = cci_job[template_name]
        name = cci_job.get("name", template_name)
        gh_job = {
            "name": name,
            "runs-on": "ubuntu-latest",
        }
        self.expand_template(gh_job, template_name, parameters, cci_job)
        if executor := self.cci_job_templates[template_name].get("executor"):
            self.set_executor(gh_job, executor, parameters)
        if requires := cci_job.get("requires"):
            gh_job["needs"] = requires
        return name, gh_job

    def set_executor(self, gh_job, executor_name, pipeline_parameters):
        if containers := self.cci_executors[executor_name].get("docker"):
            main = containers[0]
            gh_job["container"] = {
                "image": image_map(
                    replace_parameters(main["image"], {"pipeline": pipeline_parameters})
                )
            }
            if env := main.get("environment"):
                gh_job["env"] = env
            if containers[1:]:
                gh_job["services"] = {
                    container["name"]: {
                        "image": image_map(
                            replace_parameters(
                                container["image"], {"pipeline": pipeline_parameters}
                            )
                        ),
                        "env": container.get("environment", {}),
                    }
                    for container in containers[1:]
                }
        else:
            print(f"non container executor for {gh_job['name']}")
        if workdir := self.cci_executors[executor_name].get("working_directory"):
            if workdir != "~/tetrate":
                gh_job["defaults"] = {"run": {"working-directory": fix_path(workdir)}}

    def expand_template(
        self, gh_job, template_name, workflow_parameters, job_parameters
    ):
        # steps = ssh_actions.copy()
        steps = []
        template = self.cci_job_templates[template_name]
        if env := template.get("environment"):
            gh_job["env"] = env
        for step in template["steps"]:
            steps += self.expand_step(step, workflow_parameters, job_parameters, {})
        gh_job["steps"] = steps

    def expand_step(
        self,
        step,
        workflow_parameters=None,
        job_parameters=None,
        parent_step_parameters=None,
        conditional=None,
    ):
        # if step.split('/')[0] in cci_orbs:
        if type(step) == str:
            match step:
                case _ if "/" in step:
                    return [{"uses": step}]  # is orb
                case _ if step in self.cci_commands:
                    # return expand_command(step, workflow_parameters, job_parameters, {})
                    return [{"uses": f"./.github/actions/{step}"}]
                case "checkout":
                    return [{"uses": "actions/checkout@v3"}] + SSH_ACTION.copy()
                case _:
                    print(f"?? {step}")
                    return []

        step_type = list(step.keys())[0]
        match step_type:
            case "when":
                # print(f"?? {step_type}")
                if conditional:
                    print("double conditional step, can't handle")
                return [
                    x
                    for s in step[step_type]["steps"]
                    for x in self.expand_step(
                        s,
                        workflow_parameters,
                        job_parameters,
                        parent_step_parameters,
                        conditional=step[step_type]["condition"],
                    )
                ]

            case "run":
                if type(step["run"]) == str:
                    return [{"run": step["run"], "shell": "bash"}]
                run = {
                    "run": step["run"]["command"],
                    "shell": "bash",
                }
                if name := step["run"].get("name"):
                    run["name"] = name
                if env := step["run"].get("environment"):
                    run["env"] = env
                return [run]
            case _ if "/" in step_type:
                return [{"uses": step_type, "with": step[step_type]}]
            case "persist_to_workspace":
                artifacts = []
                for path in step[step_type]["paths"]:
                    artifacts += [
                        {
                            "run": f"tar zcvf archive.tar.gz {step[step_type]['root']}/{path}",
                        },
                        {
                            "uses": "actions/upload-artifact@v3",
                            "with": {
                                "path": "archive.tar.gz",
                            },
                        },
                    ]
                return artifacts
            case "store_artifacts":
                return [
                    {
                        "uses": "actions/upload-artifact@v3",
                        "with": {
                            "name": "unique_name",
                            "path": step[step_type]["path"],
                        },
                    }
                ]
            case "store_test_results":
                return [
                    {
                        "uses": "actions/upload-artifact@v3",
                        "with": {
                            "name": "unique_name",
                            "path": step[step_type]["path"],
                            "note": "test result",
                        },
                    }
                ]
            case "attach_workspace":
                return [{"uses": "actions/download-artifact@v3"}]
            case "setup_remote_docker":
                return [
                    {
                        "uses": "circle_ci_magic/setup_remote_docker",
                        "with": step[step_type],
                    }
                ]
            case _ if step_type in self.cci_commands:
                return [
                    {"uses": f"./.github/actions/{step_type}", "with": step[step_type]}
                ]
                # return expand_command(
                #     step_type, workflow_parameters, job_parameters, step[step_type]
                # )
            case _:
                print(f"?? {step_type}")
                return []

    def write_command(self, command_name, github_directory):
        command = self.cci_commands[command_name]
        command_steps = command["steps"]
        action = {
            "name": command_name,
            "description": command.get("desciption", ""),
            "runs": {
                "using": "composite",
                "steps": [s for step in command_steps for s in self.expand_step(step)],
            },
        }
        if params := command.get("parameters"):
            action["inputs"] = params
        action_dir = os.path.join(github_directory, "actions", command_name)
        os.makedirs(action_dir, exist_ok=True)
        with open(os.path.join(action_dir, "action.yaml"), "w") as fh:
            YAML().dump(action, fh)


if __name__ == "__main__":
    cci_workflow_file, github_directory = sys.argv[1:3]
    print(f"Load circle ci workflow file from {cci_workflow_file}")
    with open(cci_workflow_file) as fh:
        converter = Converter.load(fh)

    converter.export(github_directory, sys.argv[3:])
    print("done")
