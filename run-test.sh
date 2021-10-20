#!/usr/bin/env bash

echo "Using coverage? ${{ inputs.useCoverage }}"
echo "::group::gevent concurrency"
CMD=".travis/zope_testrunner_gevent.py -t checkBTreesLengthStress -t check7 -t check2 -t BlobCache -t Switches --layer gevent"
if [ "${{ inputs.useCoverage }}" = "true" ]; then
  coverage run -p --concurrency=greenlet $CMD
else
  python $CMD
fi
echo "::endgroup::"

echo "::group::non-gevent tests"
if [ "${{ inputs.useCoverage }}" = "true" ]; then
  coverage run -p --concurrency=thread ${{ inputs.testCommand }} --layer "!gevent"
else
  python ${{ inputs.testCommand }} --layer "!gevent"
fi

pip uninstall -y zope.schema
python -c 'import relstorage.interfaces, relstorage.adapters.interfaces, relstorage.cache.interfaces'
echo "::endgroup::"

echo "::group::Coverage Report"
if [ "${{ inputs.useCoverage }}" = "true" ]; then
  python -m coverage combine || true
  python -m coverage report -i || true
fi
echo "::endgroup::"
