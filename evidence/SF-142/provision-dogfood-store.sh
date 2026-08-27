#!/bin/sh
# Provision a Controller store to the internal dogfood run contract.
#
# Every value here comes from contracts/internal-dogfood-run-contract.json; the
# gate ids and their source are the `dev` targets the two labs actually declare,
# read at the commit named in the source reference. Nothing is invented, and
# running this twice is the same as running it once.
set -eu
DB=${1:-dogfood.db}
RUN="python3 -m factory_controller.cli --db $DB"

$RUN project register --id factory-prototype-lab \
  --repository https://github.com/linc-abela/factory-prototype-lab.git \
  --budget 10.0 --currency USD --cap 2 --priority 100 --policy-version dogfood-1 \
  --gate dev-check --gate dev-test --gate dev-evaluate \
  --gate-source "https://github.com/linc-abela/factory-prototype-lab.git@229b923b050fe8a4450d5597d472157bd42c8647:dev" >/dev/null

$RUN project register --id factory-bug-lab \
  --repository https://github.com/linc-abela/factory-bug-lab.git \
  --budget 10.0 --currency USD --cap 2 --priority 90 --policy-version dogfood-1 \
  --gate dev-check --gate dev-test --gate dev-reproduce \
  --gate-source "https://github.com/linc-abela/factory-bug-lab.git@961a4c97d49183b5501f244ba48773d9f50953ae:dev" >/dev/null

for project in factory-prototype-lab factory-bug-lab; do
  $RUN supervisor policy --project "$project" \
    --class backlog --class maintenance --class improvement \
    --missions-per-cycle 2 --maintenance-admissions 1 --improvement-admissions 1 \
    --policy-version dogfood-1 >/dev/null
done

$RUN portfolio --concurrency 3 --aging 1800 --policy-version dogfood-1 >/dev/null
echo "provisioned $DB"
