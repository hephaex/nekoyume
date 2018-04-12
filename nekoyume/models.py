"""
Models
======

`models.py` contains every relations regarding nekoyume blockchain and
game moves.
"""

import datetime
from hashlib import sha256 as h
import os

from bencode import bencode
from bitcoin import base58
from flask_cache import Cache
from flask_sqlalchemy import SQLAlchemy
import requests
import seccure
from sqlalchemy import or_
from sqlalchemy.ext.associationproxy import association_proxy
from sqlalchemy.orm.collections import attribute_mapped_collection
import tablib

from nekoyume.exc import (InvalidBlockError,
                          InvalidMoveError,
                          InvalidNameError,
                          OutOfRandomError)
from nekoyume import hashcash


db = SQLAlchemy()
cache = Cache()


class Node(db.Model):
    """This object contains node information you know."""

    #: URL of node
    url = db.Column(db.String, primary_key=True)
    #: last connected datetime of the node
    last_connected_at = db.Column(db.DateTime, nullable=False, index=True)

    get_blocks_endpoint = '/blocks'
    post_block_endpoint = '/blocks'
    post_move_endpoint = '/moves'

    @classmethod
    def broadcast(cls,
                  endpoint: str,
                  serialized_obj: dict,
                  sent_node=None,
                  my_node=None,
                  session=db.session) -> bool:
        """
        It broadcast `serialized_obj` to every nodes you know.

        :param        endpoint: endpoint of node to broadcast
        :param  serialized_obj: object that will be broadcasted.
        :param       sent_node: sent :class:`nekoyume.models.Node`.
                                this node ignore sent node.
        :param         my_node: my :class:`nekoyume.models.Node`.
                                received node ignore my node when they
                                broadcast received object.
        """

        for node in session.query(cls):
            if sent_node and sent_node.url == node.url:
                continue
            try:
                if my_node:
                    serialized_obj['sent_node'] = my_node.url
                requests.post(node.url + endpoint, json=serialized_obj)
                node.last_connected_at = datetime.datetime.now()
                session.add(node)
            except requests.exceptions.ConnectionError:
                continue

        session.commit()
        return True


class Block(db.Model):
    """This object contains block information."""

    __tablename__ = 'block'
    #: block id
    id = db.Column(db.Integer, primary_key=True)
    #: current block's hash
    hash = db.Column(db.String, nullable=False, index=True, unique=True)
    #: previous block's hash
    prev_hash = db.Column(db.String,
                          index=True)
    #: block creator's address
    creator = db.Column(db.String, nullable=False, index=True)
    #: hash of every linked move's ordered hash list
    root_hash = db.Column(db.String, nullable=False)
    #: suffix for hashcash
    suffix = db.Column(db.String, nullable=False)
    #: difficulty of hashcash
    difficulty = db.Column(db.Integer, nullable=False)
    #: block creation datetime
    created_at = db.Column(db.DateTime, nullable=False,
                           default=datetime.datetime.now())

    @property
    def valid(self) -> bool:
        stamp = self.serialize().decode('utf-8') + self.suffix
        valid = (self.hash == h(str.encode(stamp)).hexdigest())
        valid = valid and hashcash.check(stamp, self.suffix, self.difficulty)
        """ This function checks if the block this valid. """
        for move in self.moves:
            valid = valid and move.valid
        return valid

    def serialize(self,
                  use_bencode: bool=True,
                  include_suffix: bool=False,
                  include_moves: bool=False,
                  include_hash: bool=False):
        """
        This function serialize block.

        :param    use_bencode: check if you want to encode using bencode.
        :param include_suffix: check if you want to include suffix.
        :param  include_moves: check if you want to include linked moves.
        :param   include_hash: check if you want to include block hash.
        """
        serialized = dict(
            id=self.id,
            creator=self.creator,
            prev_hash=self.prev_hash,
            difficulty=self.difficulty,
            root_hash=self.root_hash,
            created_at=str(self.created_at),
        )
        if include_suffix:
            serialized['suffix'] = self.suffix

        if include_moves:
            serialized['moves'] = [m.serialize(
                use_bencode=False,
                include_signature=True,
                include_id=True,
            ) for m in self.moves]

        if include_hash:
            serialized['hash'] = self.hash

        if use_bencode:
            if self.prev_hash is None:
                del serialized['prev_hash']
            serialized = bencode(serialized)
        return serialized

    def broadcast(self,
                  sent_node: bool=None,
                  my_node: bool=None,
                  session=db.session) -> bool:
        """
        It broadcast this block to every nodes you know.

       :param       sent_node: sent :class:`nekoyume.models.Node`.
                               this node ignore sent node.
       :param         my_node: my :class:`nekoyume.models.Node`.
                               received node ignore my node when they
                               broadcast received object.
        """
        return Node.broadcast(Node.post_block_endpoint,
                              self.serialize(False, True, True, True),
                              sent_node, my_node, session)

    @classmethod
    def sync(cls, node: Node, session=db.session) -> bool:
        """
        Sync blockchain with other node.

        :param node: sync target :class:`nekoyume.models.Node`.
        """
        if not node:
            return True
        response = requests.get(f"{node.url}{Node.get_blocks_endpoint}/last")
        last_block = session.query(Block).order_by(Block.id.desc()).first()
        node_last_block = response.json()['block']

        if not node_last_block:
            return True

        #: If my chain is the longest one, we don't need to do anything.
        if last_block and last_block.id >= node_last_block['id']:
            return True

        def find_branch_point(value: int, high: int):
            mid = int((value + high) / 2)
            response = requests.get((f"{node.url}{Node.get_blocks_endpoint}/"
                                     f"{mid}"))
            block = session.query(Block).get(mid)
            if value > high:
                return 0
            if (response.json()['block'] and
               block.hash == response.json()['block']['hash']):
                if value == mid:
                        return value
                return find_branch_point(mid, high)
            else:
                return find_branch_point(value, mid - 1)

        if last_block:
            # TODO: Very hard to understand. fix this easily.
            if find_branch_point(last_block.id,
                                 last_block.id) == last_block.id:
                branch_point = last_block.id
            else:
                branch_point = find_branch_point(0, last_block.id)
        else:
            branch_point = 0

        for block in session.query(Block).filter(Block.id > branch_point):
            for move in block.moves:
                move.block_id = None
            session.delete(block)

        response = requests.get(f"{node.url}{Node.get_blocks_endpoint}",
                                params={'from': branch_point + 1})

        for new_block in response.json()['blocks']:
            block = Block()
            block.id = new_block['id']
            block.creator = new_block['creator']
            block.created_at = datetime.datetime.strptime(
                new_block['created_at'], '%Y-%m-%d %H:%M:%S.%f')
            block.prev_hash = new_block['prev_hash']
            block.hash = new_block['hash']
            block.difficulty = new_block['difficulty']
            block.suffix = new_block['suffix']
            block.root_hash = new_block['root_hash']

            for new_move in new_block['moves']:
                move = session.query(Move).get(new_move['id'])
                if not move:
                    move = Move(
                        id=new_move['id'],
                        user=new_move['user'],
                        name=new_move['name'],
                        signature=new_move['signature'],
                        tax=new_move['tax'],
                        details=new_move['details'],
                        created_at=datetime.datetime.strptime(
                            new_move['created_at'],
                            '%Y-%m-%d %H:%M:%S.%f'),
                        block_id=block.id,
                    )
                if not move.valid:
                    session.rollback()
                    raise InvalidMoveError
                session.add(move)

            if not block.valid:
                session.rollback()
                raise InvalidBlockError
            session.add(block)

        session.commit()
        return True


def get_address(public_key):
    return base58.encode(public_key)


class Move(db.Model):
    """This object contain general move information."""
    __tablename__ = 'move'
    #: move's hash
    id = db.Column(db.String, primary_key=True)
    #: move's block id. if the move isn't confirmed yet, this will be null
    block_id = db.Column(db.Integer, db.ForeignKey('block.id'),
                         nullable=True, index=True)
    #: move's block
    block = db.relationship('Block', uselist=False, backref='moves')
    #: move's owner
    user = db.Column(db.String, nullable=False, index=True)
    #: move's signature
    signature = db.Column(db.String, nullable=False)
    #: move name
    name = db.Column(db.String, nullable=False, index=True)
    #: move details. it contains parameters of move
    details = association_proxy(
        'move_details', 'value',
        creator=lambda k, v: MoveDetail(key=k, value=v)
    )
    #: move tax (not implemented yet)
    tax = db.Column(db.BigInteger, default=0, nullable=False)
    #: move creation datetime.
    created_at = db.Column(db.DateTime, nullable=False,
                           default=datetime.datetime.now())

    __mapper_args__ = {
        'polymorphic_identity': 'move',
        'polymorphic_on': name,
    }

    @property
    def valid(self):
        """Check if this object is valid or not"""
        if not self.signature or self.signature.find(' ') < 0:
            return False

        public_key = self.signature.split(' ')[1]
        valid = True

        valid = valid and seccure.verify(
            self.serialize(include_signature=False),
            self.signature.split(' ')[0],
            public_key,
        )
        valid = valid and (
            self.user == get_address(public_key.encode('utf-8'))
        )

        valid = valid and (self.id == self.hash)

        return valid

    @property
    def confirmed(self):
        """Check if this object is confirmed or not"""
        return self.block and self.block.hash is not None

    def serialize(self,
                  use_bencode=True,
                  include_signature=False,
                  include_id=False,
                  include_block=False):
        """
        This function serialize block.

        :param       use_bencode: check if you want to encode using bencode.
        :param include_signature: check if you want to include signature.
        :param        include_id: check if you want to include linked moves.
        :param     include_block: check if you want to include block.
        """
        serialized = dict(
            user=self.user,
            name=self.name,
            details={k: str(v) for k, v in dict(self.details).items()},
            tax=self.tax,
            created_at=str(self.created_at),
        )
        if include_signature:
            serialized['signature'] = self.signature
        if include_id:
            serialized['id'] = self.id
        if include_block:
            if self.block:
                serialized['block'] = self.block.serialize(False)
            else:
                serialized['block'] = None
        if use_bencode:
            serialized = bencode(serialized)
        return serialized

    def broadcast(self, sent_node=None, my_node=None, session=db.session):
        """
        It broadcast this move to every nodes you know.

       :param       sent_node: sent :class:`nekoyume.models.Node`.
                               this node ignore sent node.
       :param         my_node: my :class:`nekoyume.models.Node`.
                               received node ignore my node when they
                               broadcast received object.
        """
        Node.broadcast(Node.post_move_endpoint,
                       self.serialize(False, True, True),
                       sent_node, my_node, session)

    @property
    def hash(self) -> str:
        """ Get move hash """
        return h(self.serialize(include_signature=True)).hexdigest()

    def get_randoms(self) -> list:
        """ get random numbers by :doc:`Hash random <white_paper>` """
        if not (self.block and self.block.hash and self.id):
            return []
        result = [ord(a) ^ ord(b) for a, b in zip(self.block.hash, self.id)]
        result = result[int(self.block.difficulty / 4):]
        return result

    def roll(self, randoms: list, dice: str, combine=True):
        """
        Roll dices based on given randoms

            >>> from nekoyume.models import Move
            >>> move = Move()
            >>> move.roll([1, 7, 3], '2d6')
            6

        :params randoms: random numbers from
                         :func:`nekoyume.models.Move.get_randoms`
        :params    dice: dice to roll (e.g. 2d6)
        :params combine: return combined result or not if rolling it multiple.
        """
        result = []
        if dice.find('+') > 0:
            dice, plus = dice.split('+')
            plus = int(plus)
        else:
            plus = 0
        cnt, dice_type = (int(i) for i in dice.split('d'))
        for i in range(cnt):
            try:
                result.append(randoms.pop() % dice_type + 1)
            except IndexError:
                raise OutOfRandomError
        if combine:
            return sum(result) + plus
        else:
            return result


class MoveDetail(db.Model):
    move_id = db.Column(db.String,  db.ForeignKey('move.id'),
                        nullable=True, primary_key=True)
    move = db.relationship(Move, backref=db.backref(
        'move_details',
        collection_class=attribute_mapped_collection("key"),
        cascade="all, delete-orphan"
    ))
    key = db.Column(db.String, nullable=False, primary_key=True)
    value = db.Column(db.String, nullable=False, index=True)


class HackAndSlash(Move):
    __mapper_args__ = {
        'polymorphic_identity': 'hack_and_slash',
    }

    def execute(self, avatar=None):
        if not avatar:
            avatar = Avatar.get(self.user, self.block_id - 1)
        dirname = os.path.dirname(__file__)
        filename = os.path.join(dirname, 'data/monsters.csv')
        monsters = tablib.Dataset().load(
            open(filename).read()
        ).dict
        randoms = self.get_randoms()
        monster = monsters[randoms.pop() % len(monsters)]
        battle_status = []

        for key in ('hp', 'piercing', 'armor'):
            monster[key] = int(monster[key])

        while True:
            try:
                if (avatar.hp <= avatar.max_hp * 0.2
                   and 'BNDG' in avatar.items and avatar.items['BNDG'] > 0):
                    rolled = self.roll(randoms, '2d6')
                    if rolled >= 7:
                        avatar.hp += 4
                        avatar.items['BNDG'] -= 1
                        battle_status.append({
                            'type': 'item_use',
                            'item': 'BNDG',
                            'status_change': 'HP +4'
                        })
                    else:
                        avatar.items['BNDG'] -= 1
                        battle_status.append({
                            'type': 'item_use_fail',
                            'item': 'BNDG',
                            'status_change': ''
                        })

                rolled = (self.roll(randoms, '2d6')
                          + avatar.modifier('strength'))
                if rolled >= 7:
                    damage = max(
                        self.roll(randoms, avatar.damage) - monster['armor'], 0
                    )
                    battle_status.append({
                        'type': 'attack_monster',
                        'damage': damage,
                        'monster': monster.copy(),
                    })
                    monster['hp'] = monster['hp'] - damage

                elif rolled in (2, 3, 4, 5, 6, 7, 8, 9):
                    monster_damage = self.roll(randoms, monster['damage'])
                    battle_status.append({
                        'type': 'attacked_by_monster',
                        'damage': monster_damage,
                        'monster': monster.copy(),
                    })
                    avatar.hp -= monster_damage
                    if rolled <= 6:
                        battle_status.append({
                            'type': 'get_xp',
                        })
                        avatar.xp += 1

                if monster['hp'] <= 0:
                    battle_status.append({
                        'type': 'kill_monster',
                        'monster': monster.copy(),
                    })
                    reward_code = self.roll(randoms, '1d10')
                    if len(monster[f'reward{reward_code}']):
                        avatar.get_item(monster[f'reward{reward_code}'])
                        battle_status.append({
                            'type': 'get_item',
                            'item': monster[f'reward{reward_code}'],
                        })
                    return (avatar, dict(
                        type='hack_and_slash',
                        result='win',
                        battle_status=battle_status,
                    ))

                if avatar.hp <= 0:
                    battle_status.append({
                        'type': 'killed_by_monster',
                        'monster': monster.copy(),
                    })
                    return (avatar, dict(
                        type='hack_and_slash',
                        result='lose',
                        battle_status=battle_status,
                    ))

            except OutOfRandomError:
                battle_status.append({
                    'type': 'run',
                    'monster': monster.copy(),
                })
                return (avatar, dict(
                    type='hack_and_slash',
                    result='finish',
                    battle_status=battle_status,
                ))


class Sleep(Move):
    __mapper_args__ = {
        'polymorphic_identity': 'sleep',
    }

    def execute(self, avatar=None):
        if not avatar:
            avatar = Avatar.get(self.user, self.block_id - 1)
        avatar.hp = avatar.max_hp
        return avatar, dict(
            type='sleep',
            result='success',
        )


class CreateNovice(Move):
    __mapper_args__ = {
        'polymorphic_identity': 'create_novice',
    }

    def execute(self, avatar=None):
        if avatar:
            #: Keep the information that should not be removed.
            gold = avatar.items['GOLD']
        else:
            gold = 0
        avatar = Novice()

        avatar.strength = int(self.details['strength'])
        avatar.dexterity = int(self.details['dexterity'])
        avatar.constitution = int(self.details['constitution'])
        avatar.intelligence = int(self.details['intelligence'])
        avatar.wisdom = int(self.details['wisdom'])
        avatar.charisma = int(self.details['charisma'])

        if 'name' in self.details:
            avatar.name = self.details['name']
        else:
            avatar.name = self.user[:6]

        if 'gravatar_hash' in self.details:
            avatar.gravatar_hash = self.details['gravatar_hash']
        else:
            avatar.gravatar_hash = 'HASH'

        avatar.user = self.user
        avatar.current_block = self.block
        avatar.hp = avatar.max_hp
        avatar.xp = 0
        avatar.lv = 1
        avatar.items = dict(
            GOLD=gold
        )

        return (avatar, dict(
            type='create_novice',
            result='success',
        ))


class LevelUp(Move):
    __mapper_args__ = {
        'polymorphic_identity': 'level_up',
    }

    def execute(self, avatar=None):
        if not avatar:
            avatar = Avatar.get(self.user, self.block_id - 1)
        if avatar.xp < avatar.lv + 7:
            return avatar, dict(
                type='level_up',
                result='failed',
                message="You don't have enough xp.",
            )

        avatar.xp -= avatar.lv + 7
        avatar.lv += 1
        setattr(avatar, self.details['new_status'],
                getattr(avatar, self.details['new_status']) + 1)
        if self.details['new_status'] == 'constitution':
            avatar.hp += 1
        return avatar, dict(
            type='level_up',
            result='success',
        )


class Say(Move):
    __mapper_args__ = {
        'polymorphic_identity': 'say',
    }

    def execute(self, avatar=None):
        if not avatar:
            avatar = Avatar.get(self.user, self.block_id - 1)

        return avatar, dict(
            type='say',
            message=self.details['content'],
        )


class Send(Move):
    __mapper_args__ = {
        'polymorphic_identity': 'send',
    }

    def execute(self, avatar=None):
        if not avatar:
            avatar = Avatar.get(self.user, self.block_id - 1)

        if (self.details['item_name'] not in avatar.items or
           avatar.items[self.details['item_name']]
           - int(self.details['amount']) < 0):
            return avatar, dict(
                type='send',
                result='fail',
                message="You don't have enough items to send."
            )

        avatar.items[self.details['item_name']] -= int(self.details['amount'])
        return avatar, dict(
            type='send',
            result='success',
        )

    def receive(self, receiver=None):
        if not receiver:
            receiver = Avatar.get(self.details['receiver'], self.block_id - 1)

        for i in range(int(self.details['amount'])):
            receiver.get_item(self.details['item_name'])

        return receiver, dict(
            type='receive',
            result='success',
        )


class Combine(Move):
    __mapper_args__ = {
        'polymorphic_identity': 'combine',
    }

    recipes = {
        'OYKD': {'RICE', 'EGGS', 'CHKN'},
        'CBNR': {'WHET', 'EGGS', 'MEAT'},
        'STKD': {'RICE', 'RKST', 'MEAT'},
        'CHKR': {'RICE', 'RKST', 'CHKN'},
        'STEK': {'MEAT', 'RKST', 'OLIV'},
        'STCB': {'STEK', 'WHET', 'EGGS'},
        'FRCH': {'CHKN', 'RKST', 'OLIV'},
        'FSWD': {'LSWD', 'FLNT', 'OLIV'},
        'FSW1': {'FSWD', 'FSWD', 'FSWD'},
        'FSW2': {'FSW1', 'FSW1', 'FSW1'},
        'FSW3': {'FSW2', 'FSW2', 'FSW2'},
    }

    success_roll = {
        'OYKD': '1d1',
        'CBNR': '1d1',
        'STKD': '1d1',
        'CHKR': '1d1',
        'STEK': '1d1',
        'STCB': '1d1',
        'FRCH': '1d1',
        'FSWD': '1d2',
        'FSW1': '1d2',
        'FSW2': '1d4',
        'FSW3': '1d6',
    }

    def execute(self, avatar=None):
        if not avatar:
            avatar = Avatar.get(self.user, self.block_id - 1)
        randoms = self.get_randoms()
        for result, recipe in self.recipes.items():
            if recipe == {self.details['item1'],
                          self.details['item2'],
                          self.details['item3']}:
                avatar.items[self.details['item1']] -= 1
                avatar.items[self.details['item2']] -= 1
                avatar.items[self.details['item3']] -= 1
                if self.roll(randoms, self.success_roll[result]) == 1:
                    avatar.get_item(result)
                    return avatar, dict(
                        type='combine',
                        result='success',
                        result_item=result,
                    )
                else:
                    return avatar, dict(
                        type='combine',
                        result='failure',
                    )

        return avatar, dict(
            type='combine',
            result='failure',
        )


class Sell(Move):
    __mapper_args__ = {
        'polymorphic_identity': 'sell',
    }


class Buy(Move):
    __mapper_args__ = {
        'polymorphic_identity': 'buy',
    }


class User():
    def __init__(self, private_key, session=db.session):
        self.private_key = private_key
        self.public_key = str(seccure.passphrase_to_pubkey(
            self.private_key.encode('utf-8')
        ))
        self.session = session

    @property
    def address(self):
        return get_address(self.public_key.encode('utf-8'))

    def sign(self, move):
        if move.name is None:
            raise InvalidNameError
        move.user = self.address
        serialized = move.serialize(include_signature=False)
        move.signature = '{signature} {public_key}'.format(
            signature=seccure.sign(
                serialized,
                self.private_key.encode('utf-8')
            ).decode('utf-8'),
            public_key=self.public_key,
        )
        move.id = move.hash

    @property
    def moves(self):
        return self.session.query(Move).filter_by(user=self.address).filter(
            Move.block != None # noqa
        ).order_by(Move.created_at.desc())

    def move(self, new_move, tax=0, commit=True):
        new_move.user = self.address
        new_move.tax = tax
        new_move.created_at = datetime.datetime.now()
        self.sign(new_move)

        if new_move.valid:
            if commit:
                self.session.add(new_move)
                self.session.commit()
        else:
            raise InvalidMoveError

        return new_move

    def hack_and_slash(self, spot=''):
        return self.move(HackAndSlash(details={'spot': spot}))

    def sleep(self, spot=''):
        return self.move(Sleep())

    def send(self, item_name, amount, receiver):
        return self.move(Send(details={
            'item_name': item_name,
            'amount': amount,
            'receiver': receiver,
        }))

    def sell(self, item_name, price):
        return self.move(Sell(details={'item_name': item_name,
                                       'price': price}))

    def buy(self, move_id):
        return self.move(Buy(details={'move_id': move_id}))

    def create_novice(self, details):
        return self.move(CreateNovice(details=details))

    def level_up(self, new_status):
        return self.move(LevelUp(details={
            'new_status': new_status,
        }))

    def say(self, content):
        return self.move(Say(details={'content': content}))

    def combine(self, item1, item2, item3):
        return self.move(Combine(details={'item1': item1,
                                          'item2': item2,
                                          'item3': item3}))

    def create_block(self, moves, commit=True):
        for move in moves:
            if not move.valid:
                raise InvalidMoveError(move)
        # TODO: Need to add block size limit
        block = Block()
        block.root_hash = h(
            ''.join(sorted((m.id for m in moves))).encode('utf-8')
        ).hexdigest()
        block.creator = self.address
        block.created_at = datetime.datetime.now()

        prev_block = self.session.query(Block).order_by(
            Block.id.desc()
        ).first()
        if prev_block:
            block.id = prev_block.id + 1
            block.prev_hash = prev_block.hash
            block.difficulty = prev_block.difficulty
            difficulty_check_block = self.session.query(Block).get(
                max(1, block.id - 10)
            )
            avg_timedelta = (
                (block.created_at - difficulty_check_block.created_at) /
                (block.id - difficulty_check_block.id)
            )
            print(avg_timedelta, block.difficulty)
            if avg_timedelta <= datetime.timedelta(0, 5):
                block.difficulty = block.difficulty + 1
            elif avg_timedelta > datetime.timedelta(0, 15):
                block.difficulty = block.difficulty - 1
        else:
            #: Genesis block
            block.id = 1
            block.prev_hash = None
            block.difficulty = 0

        block.suffix = hashcash._mint(
            block.serialize().decode('utf-8'),
            bits=block.difficulty
        )
        if self.session.query(Block).get(block.id):
            return None
        block.hash = h(
            (block.serialize().decode('utf-8') + block.suffix).encode('utf-8')
        ).hexdigest()

        for move in moves:
            move.block = block

        if commit:
            self.session.add(block)
            self.session.commit()

        return block

    def avatar(self, block_id=None):
        if not block_id:
            block = self.session.query(Block).order_by(
                Block.id.desc()).first()
            if block:
                block_id = block.id
            else:
                block_id = 0
        return Avatar.get(self.address, block_id)


class Avatar():
    @classmethod
    @cache.memoize()
    def get(cls, user_addr, block_id, session=db.session):
        create_move = session.query(Move).filter_by(user=user_addr).filter(
            Move.block_id <= block_id
        ).order_by(
            Move.block_id.desc()
        ).filter(
            Move.name.like('create_%')
        ).first()
        if not create_move or block_id < create_move.block_id:
            return None
        moves = session.query(Move).filter(
            or_(Move.user == user_addr, Move.id.in_(
                    db.session.query(MoveDetail.move_id).filter_by(
                        key='receiver', value=user_addr)))
        ).filter(
            Move.block_id >= create_move.block_id,
            Move.block_id <= block_id
        )
        avatar, result = create_move.execute(None)
        avatar.items['GOLD'] += session.query(Block).filter_by(
            creator=user_addr
        ).filter(Block.id <= block_id).count() * 8

        for move in moves:
            if move.user == user_addr:
                avatar, result = move.execute(avatar)
            if (type(move) == Send and
               move.details['receiver'] == user_addr):
                avatar, result = move.receive(avatar)

        return avatar

    def modifier(self, status):
        status = getattr(self, status)
        if status in (1, 2, 3):
            return -3
        elif status in (4, 5):
            return -2
        elif status in (6, 7, 8):
            return -1
        elif status in (9, 10, 11, 12):
            return 0
        elif status in (13, 14, 15):
            return 1
        elif status in (16, 17):
            return 2
        elif status == 18:
            return 3
        return 0

    def get_item(self, item):
        if item not in self.items:
            self.items[item] = 1
        else:
            self.items[item] += 1

    @property
    def damage(self):
        raise NotImplementedError

    @property
    def max_hp(self):
        raise NotImplementedError

    @property
    def profile_image_url(self):
        return f'https://www.gravatar.com/avatar/{self.gravatar_hash}?d=mm'


class Novice(Avatar):
    @property
    def damage(self):
        return '1d6'

    @property
    def max_hp(self):
        return self.constitution + 6
