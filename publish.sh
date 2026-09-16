sh build.sh

docker image prune -f

docker save -o bing.tar bing-img-proxy:latest && scp bing.tar qiang-tc2028:/home/bing-img-proxy && rm -f bing.tar

