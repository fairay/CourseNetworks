import logging
import bitstring
import math
import os
import time
import random

from collections import defaultdict
from typing import List, Dict, Optional
from hashlib import sha1

from torfile import Torrent
from peer_msg import REQUEST_SIZE


class Block:
    Missing = 0
    Pending = 1
    Retrieved = 2

    data: bytes = None

    def __init__(self, piece: int, offset: int, length: int):
        self.piece = piece
        self.offset = offset
        self.length = length
        self.status = self.Missing
        self.last_usage: float = -1


class Piece:
    def __init__(self, index: int, blocks: List[Block], hash_value: bytes):
        self.index = index
        self.blocks = blocks
        self.hash = hash_value

    def next_request(self) -> Optional[Block]:
        for block in self.blocks:
            if block.status is Block.Missing:
                block.status = Block.Pending
                return block
        return None

    def block_received(self, offset: int, data: bytes):
        for b in self.blocks:
            if b.offset == offset:
                b.status = Block.Retrieved
                b.data = data
                break
        else:
            logging.warning('Trying to complete a non-existing block {}'.format(offset))

    def is_complete(self) -> bool:
        for b in self.blocks:
            if b.status != Block.Retrieved:
                return False

        return True

    def is_hash_matching(self) -> bool:
        data_hash = sha1(self.data).digest()
        return data_hash == self.hash

    def clear_data(self):
        del self.blocks
        self.blocks = []

    @property
    def data(self):
        retrieved = sorted(self.blocks, key=lambda b: b.offset)
        blocks_data = [b.data for b in retrieved]
        return b''.join(blocks_data)

    @property
    def loaded_bytes(self):
        return sum(b.length if b.status is Block.Retrieved
                   else 0
                   for b in self.blocks)


class PieceManager:
    missing_pieces: List[Piece] = []            # Pieces которых нет совсем
    ongoing_pieces: List[Piece] = []            # Pieces в процессе загрузки
    # have_pieces: List[Piece] = []               # Pieces загруженные

    piecemap: bitstring.BitArray
    peers: Dict[str, bitstring.BitArray] = {}   # Пара id пира: бит-карта pieces
    pending_blocks: List[Block] = []            # Блоки в процессе загрузки
    max_pending_time = 1 * 60  # one minute

    def __init__(self, torrent: Torrent, file_path: str):
        self.torrent = torrent
        self.missing_pieces = self._initiate_pieces()
        self._file_init(file_path)

    def _initiate_pieces(self) -> [Piece]:
        pieces: List[Piece] = []
        num_pieces = len(self.torrent.pieces)
        self.piecemap = bitstring.BitArray(num_pieces)

        piece_block_n = math.ceil(self.torrent.piece_length / REQUEST_SIZE)

        for index, hash_value in enumerate(self.torrent.pieces):
            if index < num_pieces - 1:
                blocks = [Block(index, offset * REQUEST_SIZE, REQUEST_SIZE)
                          for offset in range(piece_block_n)]
            else:
                last_length = self.torrent.total_size - self.torrent.piece_length * index
                num_blocks = math.ceil(last_length / REQUEST_SIZE)
                blocks = [Block(index, offset * REQUEST_SIZE, REQUEST_SIZE)
                          for offset in range(num_blocks)]

                if last_length % REQUEST_SIZE:
                    blocks[-1].length = last_length % REQUEST_SIZE

            pieces.append(Piece(index, blocks, hash_value))

        return pieces

    def close(self):
        if self.fd:
            os.close(self.fd)

    def add_peer(self, peer_id: str, bitfield: bitstring.BitArray):
        self.peers[peer_id] = bitfield

        logging.debug('add new peer to PieceManager\n {} {}'.format(peer_id, bitfield))

    def update_peer(self, peer_id: str, index: int):
        if peer_id in self.peers:
            self.peers[peer_id][index] = 1

        logging.debug('update peer in PieceManager')

    def next_request(self, peer_id: str) -> Optional[Block]:
        if peer_id not in self.peers:
            return None

        block = self._expired_requests(peer_id)
        if block is not None:
            return block

        block = self._next_ongoing(peer_id)
        if block is not None:
            return block

        piece = self._get_rarest_piece(peer_id)
        if piece is None:
            return self._oldest_request(peer_id)
        else:
            return self._next_ongoing(peer_id)

    def block_received(self, peer_id: str, piece_index: int, block_offset, data):
        logging.debug('Received block {0} for piece {1} from peer {2}: '.
                      format(block_offset, piece_index, peer_id))

        for index, request in enumerate(self.pending_blocks):
            if request.piece == piece_index and request.offset == block_offset:
                del self.pending_blocks[index]
                break

        for p in self.ongoing_pieces:
            if p.index == piece_index:
                piece = p
                break
        else:
            logging.warning('Trying to update piece {} that is not ongoing!'.format(piece_index))
            return

        piece.block_received(block_offset, data)
        if not piece.is_complete():
            return

        if piece.is_hash_matching():
            self._piece_loaded(piece)
        else:
            logging.warning('Piece {} corrupted, refetching'.
                            format(piece.index))

    def _piece_loaded(self, piece: Piece):
        self._write(piece)

        self.ongoing_pieces.remove(piece)
        piece.clear_data()
        # self.have_pieces.append(piece)
        self.piecemap[piece.index] = True
        print('Piece {} downloaded, {:.2%} done'.
              format(piece.index,
                     sum(self.piecemap) / len(self.piecemap)))

        logging.info('Piece {} downloaded, {:.2%} done'.
                     format(piece.index,
                            sum(self.piecemap) / len(self.piecemap)))

    def _oldest_request(self, peer_id: str) -> Optional[Block]:
        if not self.pending_blocks:
            return None

        latest: Optional[Block] = None
        for request in self.pending_blocks:
            if self.peers[peer_id][request.piece] and \
                    (latest is None or request.last_usage < latest.last_usage):
                latest = request

        if latest is not None:
            logging.info('Re-requesting block {} for piece {}'.format(latest.offset, latest.piece))
            latest.last_usage = time.time()
        return latest

    def _expired_requests(self, peer_id: str) -> Optional[Block]:
        current = time.time()

        for request in self.pending_blocks:
            if self.peers[peer_id][request.piece] and\
                    current - request.last_usage > self.max_pending_time:
                logging.info('Re-requesting block {} for piece {}'.format(request.offset, request.piece))
                request.last_usage = current
                return request

        return None

    def _next_ongoing(self, peer_id: str) -> Optional[Block]:
        for piece in self.ongoing_pieces:
            if self.peers[peer_id][piece.index]:
                block = piece.next_request()
                if block is None:
                    continue

                block.last_usage = time.time()
                self.pending_blocks.append(block)
                return block
        return None

    def _get_rarest_piece(self, peer_id: str) -> Optional[Piece]:
        piece_count = defaultdict(int)

        missing_list = self.missing_pieces[:]
        random.shuffle(missing_list)
        for piece in missing_list:
            if not self.peers[peer_id][piece.index]:
                continue
            for peer in self.peers:
                if self.peers[peer][piece.index]:
                    piece_count[piece] += 1

        if not piece_count:
            return None

        rarest_piece = min(piece_count, key=lambda p: piece_count[p])

        self.missing_pieces.remove(rarest_piece)
        self.ongoing_pieces.append(rarest_piece)

        return rarest_piece

    def _write(self, piece: Piece):
        pos = piece.index * self.torrent.piece_length
        os.lseek(self.fd, pos, os.SEEK_SET)
        os.write(self.fd, piece.data)

    def _file_init(self, file_path: str):
        self.file_path = file_path + '/' + self.torrent.output_file
        logging.info('Loading in {}'.format(self.file_path))

        self.fd = os.open(self.file_path, os.O_RDWR | os.O_CREAT | os.O_TRUNC)
        pos = (len(self.torrent.pieces) - 1) * self.torrent.piece_length
        os.lseek(self.fd, pos, os.SEEK_SET)
        os.write(self.fd, b'\0')

    @property
    def complete(self) -> bool:
        return sum(self.piecemap) == len(self.piecemap)

    @property
    def loaded_bytes(self) -> int:
        having_bytes = sum(self.piecemap) * self.torrent.piece_length  # bug: last piece hasn't full length
        ongoing_bytes = sum(p.loaded_bytes for p in self.ongoing_pieces)
        return having_bytes + ongoing_bytes

    @property
    def uploaded_bytes(self) -> int:
        return 0

    @property
    def piece_n(self) -> int:
        return len(self.piecemap)

    @property
    def ongoing_map(self) -> bitstring.BitArray:
        num_pieces = len(self.torrent.pieces)
        ongoing_map = bitstring.BitArray(num_pieces)

        for p in self.ongoing_pieces:
            ongoing_map[p.index] = True

        return ongoing_map
